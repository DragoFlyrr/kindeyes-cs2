/* flashdim_hot.c
 * Native hot-path for gsi_flashdim.py.
 *
 * GSI hot path:
 *   IOCP+AcceptEx -> scan for "flashed":N -> MagSet(dark) on rising edge
 *   -> send 200 OK -> closesocket -> post next AcceptEx
 *
 * Visual hot path (primary trigger):
 *   DXGI Desktop Duplication -> AcquireNextFrame(0) -> 9-pixel sample
 *   via 3x3 staging texture -> achromatic-bright check -> MagSet(dark)
 *   if N-of-9 pixels pass.
 *
 * Either path can fire; `already_dimmed` argument prevents retrigger while
 * the fade profile is active. Visual typically lands 15-70 ms ahead of
 * GSI because CS2's GSI emit pipeline coalesces/throttles at the tail
 * of the flash, whereas DXGI sees the bright frame ~1 monitor refresh
 * after the GPU renders it.
 *
 * Thread affinity: MagSet, D3D11 device, and IOCP are all owned by the
 * thread that called hot_init + hot_visual_init. That thread must drive
 * hot_tick and hot_visual_tick.
 *
 * Build: build_hot.bat (MSVC via vcvars64)
 */
#define WIN32_LEAN_AND_MEAN
#define COBJMACROS
#define INITGUID
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <mswsock.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <functiondiscoverykeys_devpkey.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "mswsock.lib")
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "dxguid.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "uuid.lib")
#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "user32.lib")

typedef struct {
    float transform[25];
} MAGCOLOREFFECT;

typedef BOOL (WINAPI *pfn_MagSet)(const MAGCOLOREFFECT*);

static pfn_MagSet g_MagSet = NULL;
static MAGCOLOREFFECT g_dark;
static MAGCOLOREFFECT g_identity;
static MAGCOLOREFFECT g_fade;
static LARGE_INTEGER g_qpc_freq;

/* ---- Gamma ramp (scanout-path darkening) ----------------------------
 * SetDeviceGammaRamp modifies the LUT the GPU applies during scanout,
 * bypassing the DWM compositor. This means a gamma change can land on
 * the physical display ~1 frame earlier than MagSetFullscreenColorEffect
 * (which needs DWM to recomposite).
 *
 * Win10+ clamps the usable range of the gamma LUT unless the registry
 * key HKLM\...\ICM\GdiICMGammaRange is set to 0x100 (requires reboot).
 * Without the unlock we still get SOME darkening -- typically ~50-70% --
 * which is still useful as the first "prep" layer before MagSet's full
 * dim lands on the next vsync.
 *
 * Strategy: on fire, we call gamma_apply(dark) FIRST, then MagSet(dark).
 * Gamma takes effect on the next scanout (~0-16ms), MagSet one vsync
 * after that. So the flash visibility shrinks by roughly one frame.
 */
static HDC   g_gamma_hdc = NULL;
static WORD  g_gamma_orig[256 * 3];
static int   g_gamma_orig_valid = 0;
static float g_gamma_min_bright = 0.05f;  /* dark brightness multiplier */

static void gamma_build_ramp(WORD* ramp, float brightness) {
    if (brightness < 0.0f) brightness = 0.0f;
    if (brightness > 1.0f) brightness = 1.0f;
    for (int i = 0; i < 256; i++) {
        int v = (int)((float)i * 257.0f * brightness + 0.5f);
        if (v < 0) v = 0;
        if (v > 65535) v = 65535;
        ramp[i]       = (WORD)v;   /* R */
        ramp[256 + i] = (WORD)v;   /* G */
        ramp[512 + i] = (WORD)v;   /* B */
    }
}

/* ---- IOCP / AcceptEx state ------------------------------------------- */
static HANDLE g_iocp = NULL;
static SOCKET g_listen = INVALID_SOCKET;
static SOCKET g_accept = INVALID_SOCKET;
static LPFN_ACCEPTEX g_AcceptEx = NULL;

#define AX_DATA_LEN 4096
#define AX_ADDR_LEN (sizeof(SOCKADDR_IN) + 16)

static char g_accept_buf[AX_DATA_LEN + 2 * AX_ADDR_LEN];
static OVERLAPPED g_overlapped;

/* minimal HTTP 200 OK response -- Content-Length: 0, Connection: close */
static const char g_reply[] =
    "HTTP/1.1 200 OK\r\n"
    "Content-Length: 0\r\n"
    "Connection: close\r\n"
    "\r\n";
static const int g_reply_len = (int)(sizeof(g_reply) - 1);

/* ---- DXGI Desktop Duplication state --------------------------------- */
static ID3D11Device*          g_d3d     = NULL;
static ID3D11DeviceContext*   g_ctx     = NULL;
static IDXGIOutputDuplication* g_dup    = NULL;
static ID3D11Texture2D*       g_stage   = NULL;   /* 3x3 CPU-read staging */
static IDXGIOutput1*          g_output1 = NULL;   /* kept for recreation */
static int g_screen_w = 0;
static int g_screen_h = 0;
static int g_sample_x[9];
static int g_sample_y[9];
static int g_min_bright  = 5;    /* 5-of-9 default (peak gate) */
static int g_bright_min  = 150;  /* per-channel floor (peak gate) */
static int g_max_spread  = 25;   /* max(R,G,B) - min(R,G,B) <= this (peak gate) */
/* Rapid-rise ramp-detection gate. Catches the first bright frame of a
 * flashbang (RGB ~180-220) instead of waiting for the saturated peak
 * (RGB ~230+). Only fires on a frame-to-frame jump. */
static int g_relaxed_bmin   = 180;  /* relaxed per-channel floor */
static int g_relaxed_spread = 30;   /* relaxed achromatic test */
static int g_rise_min       = 5;    /* 5-of-9 relaxed must fire on ramp */
static int g_rise_prev_max  = 1;    /* prev relaxed must be <= this */
static int g_prev_relaxed   = 0;    /* carries between ticks */

/* Per-pixel delta gate. Catches flashes from bright scenes (where
 * prev_relaxed is already high, so the rise gate can't fire). Stores
 * last-frame RGB per sample point; fires if >= N pixels saw a luma
 * jump >= THRESH between frames. Walls stable -> low delta. Flashes
 * inject a huge step regardless of baseline. */
static int g_delta_min       = 8;      /* >= N pixels must cross threshold (near-fullscreen) */
static int g_delta_thresh    = 200;    /* per-pixel luma delta (0-765 sum) -- huge jump */
static int g_delta_min_cur_lum = 680;  /* current luma sum must be near-white post-delta */
static uint8_t g_prev_rgb[9 * 3];      /* R,G,B per sample point, packed */
static int g_prev_rgb_valid  = 0;

/* Audio priming. When WASAPI loopback detects a transient matching a
 * flashbang signature, we set g_audio_primed_until_qpc to a point in
 * the future (~150ms ahead). While primed, the visual scan uses looser
 * thresholds AND the delta gate lowers. No MagSet fires from audio
 * alone -- visual must still confirm with bright pixels. Audio only
 * lowers the bar so that the ramp frame is caught before saturation.
 *
 * Key user concern: "what if we hear but don't see?" -- handled: audio
 * without visual confirm => no dim. Only an already-visual flash that
 * also produced audio triggers earlier detection than a visual-only
 * frame would have.
 */
static LARGE_INTEGER g_audio_primed_until_qpc;
static int g_audio_primed_bmin   = 120;  /* per-channel floor while primed */
static int g_audio_primed_spread = 60;   /* spread while primed */
static int g_audio_primed_min    = 3;    /* 3-of-9 while primed */
static int g_audio_primed_delta_min = 3; /* 3-of-9 delta while primed (audio confirms) */
static int g_audio_primed_delta_thresh = 80; /* small delta ok while primed -- catch ramp */

/* Rapid-rise ramp-of-luma gate. Tracks max per-pixel luma sum across the
 * last 3 frames. Fires when current + last 3 are strictly monotonically
 * rising AND the total rise (ml[now] - ml[now-3]) >= RISE_MIN AND the
 * current max_lum >= CUR_LUM_MIN (near-peak floor). Catches the ramp
 * before it saturates -- PEAK/DELTA gates need near-white; this fires
 * at ~RGB 200 with clear upward momentum.
 *
 * 3-frame window = ~50ms at 60Hz, so a real flashbang ramp (which takes
 * ~85ms from scene-dark to near-white) crosses this gate ~33ms before
 * the PEAK gate would fire. False-positive guard: requires strict-
 * monotonic rise, meaning noisy/oscillating scenes (weapon swap
 * shimmers, muzzle flash flicker) naturally block it. */
static int g_maxlum_hist[3] = { 0, 0, 0 };  /* [0]=N-1, [1]=N-2, [2]=N-3 */
static int g_maxlum_hist_fill = 0;          /* count until 3 */
static int g_ramp_rise_min     = 250;       /* total rise across 3 frames */
static int g_ramp_cur_lum_min  = 720;       /* current max_lum floor */

static uint64_t g_visual_frame_n = 0;

static void make_effect(MAGCOLOREFFECT* e, float b) {
    memset(e->transform, 0, sizeof(e->transform));
    e->transform[0]  = b;
    e->transform[6]  = b;
    e->transform[12] = b;
    e->transform[18] = 1.0f;
    e->transform[24] = 1.0f;
}

static uint64_t qpc_diff_ns(LARGE_INTEGER a, LARGE_INTEGER b) {
    return (uint64_t)((b.QuadPart - a.QuadPart) * 1000000000ULL / g_qpc_freq.QuadPart);
}

static int post_accept(void) {
    g_accept = WSASocketW(AF_INET, SOCK_STREAM, IPPROTO_TCP, NULL, 0,
                          WSA_FLAG_OVERLAPPED);
    if (g_accept == INVALID_SOCKET) return -1;

    memset(&g_overlapped, 0, sizeof(g_overlapped));
    DWORD bytes = 0;
    BOOL ok = g_AcceptEx(g_listen, g_accept, g_accept_buf,
                         AX_DATA_LEN,
                         (DWORD)AX_ADDR_LEN, (DWORD)AX_ADDR_LEN,
                         &bytes, &g_overlapped);
    if (!ok) {
        int err = WSAGetLastError();
        if (err != ERROR_IO_PENDING) {
            closesocket(g_accept);
            g_accept = INVALID_SOCKET;
            return -err;
        }
    }
    return 0;
}

/* ---- exports --------------------------------------------------------- */

__declspec(dllexport)
int hot_init(float min_brightness, uint16_t port) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return -1;

    HMODULE h = LoadLibraryA("magnification.dll");
    if (!h) return -2;
    g_MagSet = (pfn_MagSet)GetProcAddress(h, "MagSetFullscreenColorEffect");
    if (!g_MagSet) return -3;
    make_effect(&g_dark, min_brightness);
    make_effect(&g_identity, 1.0f);
    make_effect(&g_fade, 1.0f);
    QueryPerformanceFrequency(&g_qpc_freq);

    g_listen = WSASocketW(AF_INET, SOCK_STREAM, IPPROTO_TCP, NULL, 0,
                          WSA_FLAG_OVERLAPPED);
    if (g_listen == INVALID_SOCKET) return -4;

    BOOL reuse = TRUE;
    setsockopt(g_listen, SOL_SOCKET, SO_REUSEADDR,
               (const char*)&reuse, sizeof(reuse));

    SOCKADDR_IN addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(port);
    if (bind(g_listen, (SOCKADDR*)&addr, sizeof(addr)) != 0) return -5;
    if (listen(g_listen, 16) != 0) return -6;

    GUID guid = WSAID_ACCEPTEX;
    DWORD bytes = 0;
    if (WSAIoctl(g_listen, SIO_GET_EXTENSION_FUNCTION_POINTER,
                 &guid, sizeof(guid),
                 &g_AcceptEx, sizeof(g_AcceptEx),
                 &bytes, NULL, NULL) != 0) return -7;

    g_iocp = CreateIoCompletionPort(INVALID_HANDLE_VALUE, NULL, 0, 0);
    if (!g_iocp) return -8;
    if (!CreateIoCompletionPort((HANDLE)g_listen, g_iocp, 1, 0)) return -9;

    int rc = post_accept();
    if (rc != 0) return -10;

    return 0;
}

__declspec(dllexport)
int hot_apply_fade(float brightness) {
    if (!g_MagSet) return -1;
    g_fade.transform[0]  = brightness;
    g_fade.transform[6]  = brightness;
    g_fade.transform[12] = brightness;
    return g_MagSet(&g_fade) ? 0 : -2;
}

__declspec(dllexport)
int hot_apply_dark(void) {
    if (!g_MagSet) return -1;
    return g_MagSet(&g_dark) ? 0 : -2;
}

__declspec(dllexport)
int hot_apply_identity(void) {
    if (!g_MagSet) return -1;
    return g_MagSet(&g_identity) ? 0 : -2;
}

/* ---- Gamma ramp exports ---------------------------------------------
 * Must be called from a thread that owns a DC. We grab the screen DC
 * with GetDC(NULL) once and keep it for the lifetime of the process.
 */
__declspec(dllexport)
int hot_gamma_init(float min_brightness) {
    if (min_brightness >= 0.0f && min_brightness <= 1.0f) {
        g_gamma_min_bright = min_brightness;
    }
    if (g_gamma_hdc == NULL) {
        g_gamma_hdc = GetDC(NULL);
        if (g_gamma_hdc == NULL) return -1;
    }
    if (!g_gamma_orig_valid) {
        if (!GetDeviceGammaRamp(g_gamma_hdc, g_gamma_orig)) {
            return -2;
        }
        g_gamma_orig_valid = 1;
    }
    return 0;
}

__declspec(dllexport)
int hot_gamma_apply_dark(void) {
    if (g_gamma_hdc == NULL) return -1;
    WORD ramp[256 * 3];
    gamma_build_ramp(ramp, g_gamma_min_bright);
    return SetDeviceGammaRamp(g_gamma_hdc, ramp) ? 0 : -2;
}

__declspec(dllexport)
int hot_gamma_apply_fade(float brightness) {
    if (g_gamma_hdc == NULL) return -1;
    WORD ramp[256 * 3];
    gamma_build_ramp(ramp, brightness);
    return SetDeviceGammaRamp(g_gamma_hdc, ramp) ? 0 : -2;
}

__declspec(dllexport)
int hot_gamma_apply_identity(void) {
    if (g_gamma_hdc == NULL) return -1;
    if (g_gamma_orig_valid) {
        return SetDeviceGammaRamp(g_gamma_hdc, g_gamma_orig) ? 0 : -2;
    }
    WORD ramp[256 * 3];
    gamma_build_ramp(ramp, 1.0f);
    return SetDeviceGammaRamp(g_gamma_hdc, ramp) ? 0 : -2;
}

typedef struct {
    int32_t  had_connection;   /* 1 if we processed a completion */
    int32_t  fired;            /* 1 if MagSet(dark) fired */
    int32_t  flashed_value;    /* parsed "flashed": value (0 if not found) */
    int32_t  bytes_read;       /* AcceptEx data length */
    uint64_t t_gqcs_ns;        /* enter -> GQCS return */
    uint64_t t_scan_ns;        /* GQCS -> scan done */
    uint64_t t_magset_ns;      /* scan -> magset return (only if fired) */
    uint64_t t_reply_ns;       /* magset/scan -> send returned */
    uint64_t t_total_ns;       /* enter -> return */
} hot_tick_t;

/* Full-cycle GSI handler.
 *   prev_flashed:   rising-edge guard (previous "flashed" int from CS2)
 *   already_dimmed: non-zero if another path already fired MagSet(dark);
 *                   we still parse+reply+advance prev, but skip MagSet
 *   out:            result struct
 * Returns 0 on success (no completion ready is NOT an error),
 *         negative WSA error otherwise.
 */
__declspec(dllexport)
int hot_tick(int32_t prev_flashed, int32_t already_dimmed, hot_tick_t* out)
{
    LARGE_INTEGER t0, t1, t2, t3, t4;
    QueryPerformanceCounter(&t0);

    out->had_connection = 0;
    out->fired = 0;
    out->flashed_value = 0;
    out->bytes_read = 0;
    out->t_gqcs_ns = 0;
    out->t_scan_ns = 0;
    out->t_magset_ns = 0;
    out->t_reply_ns = 0;
    out->t_total_ns = 0;

    DWORD bytes = 0;
    ULONG_PTR key = 0;
    LPOVERLAPPED ov = NULL;
    BOOL ok = GetQueuedCompletionStatus(g_iocp, &bytes, &key, &ov, 0);
    QueryPerformanceCounter(&t1);
    out->t_gqcs_ns = qpc_diff_ns(t0, t1);

    if (!ok) {
        DWORD err = GetLastError();
        if (err == WAIT_TIMEOUT || ov == NULL) {
            out->t_total_ns = out->t_gqcs_ns;
            return 0;
        }
        if (g_accept != INVALID_SOCKET) {
            closesocket(g_accept);
            g_accept = INVALID_SOCKET;
        }
        post_accept();
        out->t_total_ns = out->t_gqcs_ns;
        return -(int)err;
    }

    SOCKET conn = g_accept;
    g_accept = INVALID_SOCKET;
    out->had_connection = 1;

    /* AcceptEx may complete on just the first TCP segment (usually the
     * HTTP headers, ~250 bytes on CS2 GSI). The JSON body arrives in a
     * second segment a fraction of a ms later. Drain with a short-timeout
     * blocking recv so the scan sees "flashed":N. */
    setsockopt(conn, SOL_SOCKET, SO_UPDATE_ACCEPT_CONTEXT,
               (const char*)&g_listen, sizeof(g_listen));
    DWORD rtmo_ms = 5;
    setsockopt(conn, SOL_SOCKET, SO_RCVTIMEO,
               (const char*)&rtmo_ms, sizeof(rtmo_ms));
    int n = (int)bytes;
    if (n > AX_DATA_LEN) n = AX_DATA_LEN;
    int hdr_end = -1;
    for (int i = 0; i + 3 < n; i++) {
        if (g_accept_buf[i]=='\r' && g_accept_buf[i+1]=='\n' &&
            g_accept_buf[i+2]=='\r' && g_accept_buf[i+3]=='\n') {
            hdr_end = i + 4; break;
        }
    }
    while (n < AX_DATA_LEN) {
        if (hdr_end >= 0 && (n - hdr_end) >= 200) break; /* enough body */
        int rc = recv(conn, g_accept_buf + n, AX_DATA_LEN - n, 0);
        if (rc > 0) {
            n += rc;
            if (hdr_end < 0) {
                for (int i = (n - rc - 3 < 0 ? 0 : n - rc - 3); i + 3 < n; i++) {
                    if (g_accept_buf[i]=='\r' && g_accept_buf[i+1]=='\n' &&
                        g_accept_buf[i+2]=='\r' && g_accept_buf[i+3]=='\n') {
                        hdr_end = i + 4; break;
                    }
                }
            }
            continue;
        }
        break; /* 0 = closed, -1 = timeout / err */
    }
    out->bytes_read = (int32_t)n;

    /* Scan buffer for "flashed":<digit>. Fires MagSet on the
     * rising edge (prev=0, now>=1) before doing anything else. */
    int flashed_value = 0;
    int idx = -1;
    int limit = n - 10;
    for (int i = 0; i <= limit; i++) {
        const char* p = g_accept_buf + i;
        if (p[0] == '"' && p[1] == 'f' && p[2] == 'l' && p[3] == 'a' &&
            p[4] == 's' && p[5] == 'h' && p[6] == 'e' && p[7] == 'd' &&
            p[8] == '"' && p[9] == ':') {
            idx = i;
            break;
        }
    }
    if (idx >= 0) {
        int vs = idx + 10;
        while (vs < n) {
            char c = g_accept_buf[vs];
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n') vs++;
            else break;
        }
        while (vs < n && g_accept_buf[vs] >= '0' && g_accept_buf[vs] <= '9') {
            flashed_value = flashed_value * 10 + (g_accept_buf[vs] - '0');
            if (flashed_value > 255) { flashed_value = 255; break; }
            vs++;
        }
    }
    out->flashed_value = flashed_value;

    QueryPerformanceCounter(&t2);
    out->t_scan_ns = qpc_diff_ns(t1, t2);

    if (flashed_value > 0 && prev_flashed == 0 && !already_dimmed) {
        /* Gamma ramp first (scanout path, ~1 frame faster than MagSet),
         * then MagSet for the full DWM-level dim. */
        if (g_gamma_hdc != NULL) {
            WORD ramp[256 * 3];
            gamma_build_ramp(ramp, g_gamma_min_bright);
            SetDeviceGammaRamp(g_gamma_hdc, ramp);
        }
        g_MagSet(&g_dark);
        QueryPerformanceCounter(&t3);
        out->t_magset_ns = qpc_diff_ns(t2, t3);
        out->fired = 1;
    } else {
        t3 = t2;
    }

    send(conn, g_reply, g_reply_len, 0);
    closesocket(conn);
    QueryPerformanceCounter(&t4);
    out->t_reply_ns = qpc_diff_ns(t3, t4);

    post_accept();

    out->t_total_ns = qpc_diff_ns(t0, t4);
    return 0;
}

/* ======================================================================
 *   VISUAL HOT PATH: DXGI Desktop Duplication
 * ====================================================================== */

typedef struct {
    int32_t  had_frame;      /* 1 if we acquired a new frame */
    int32_t  fired;          /* 1 if MagSet(dark) fired */
    int32_t  bright_count;   /* pixels passing strict peak test, out of 9 */
    int32_t  frame_num;      /* cumulative frame counter (wraps at 2^31) */
    int32_t  relaxed_count;  /* pixels passing relaxed ramp test, out of 9 */
    int32_t  prev_relaxed;   /* relaxed count from previous frame */
    int32_t  trigger_kind;   /* 0=none, 1=peak, 2=rise, 3=delta, 4=audio-primed, 5=ramp */
    int32_t  delta_count;    /* pixels with luma delta >= threshold */
    int32_t  audio_primed;   /* 1 if visual ran with audio-primed gates */
    int32_t  max_luma;       /* max per-pixel luma sum on this frame (0-765) */
    int32_t  ramp_rise;      /* max_lum - max_lum[N-3] when ramp is rising, else 0 */
    uint64_t t_acquire_ns;   /* enter -> AcquireNextFrame return */
    uint64_t t_copy_ns;      /* acquire -> 9 CopySubresourceRegion + Release done */
    uint64_t t_scan_ns;      /* copy -> Map+scan+Unmap done */
    uint64_t t_magset_ns;    /* scan -> MagSet return (only if fired) */
    uint64_t t_total_ns;     /* enter -> return */
} hot_visual_t;

static void compute_sample_points(int w, int h) {
    int cx = w / 2;
    int cy = h / 2;
    int dx = 800;
    int dy = 600;
    if (dx > cx - 4) dx = cx - 4;
    if (dy > cy - 4) dy = cy - 4;
    if (dx < 0) dx = 0;
    if (dy < 0) dy = 0;
    int xs[3] = { cx - dx, cx, cx + dx };
    int ys[3] = { cy - dy, cy, cy + dy };
    int k = 0;
    for (int iy = 0; iy < 3; iy++) {
        for (int ix = 0; ix < 3; ix++) {
            g_sample_x[k] = xs[ix];
            g_sample_y[k] = ys[iy];
            k++;
        }
    }
}

static void dup_release_all(void) {
    if (g_dup)     { g_dup->lpVtbl->Release(g_dup); g_dup = NULL; }
    if (g_stage)   { g_stage->lpVtbl->Release(g_stage); g_stage = NULL; }
    if (g_output1) { g_output1->lpVtbl->Release(g_output1); g_output1 = NULL; }
    if (g_ctx)     { g_ctx->lpVtbl->Release(g_ctx); g_ctx = NULL; }
    if (g_d3d)     { g_d3d->lpVtbl->Release(g_d3d); g_d3d = NULL; }
}

static int dup_create(void) {
    IDXGIFactory1* factory = NULL;
    IDXGIAdapter1* adapter = NULL;
    IDXGIOutput*   output  = NULL;
    HRESULT hr;

    hr = CreateDXGIFactory1(&IID_IDXGIFactory1, (void**)&factory);
    if (FAILED(hr)) goto fail;

    hr = factory->lpVtbl->EnumAdapters1(factory, 0, &adapter);
    if (FAILED(hr)) goto fail;

    D3D_FEATURE_LEVEL fl_in[] = {
        D3D_FEATURE_LEVEL_11_0,
        D3D_FEATURE_LEVEL_10_1,
        D3D_FEATURE_LEVEL_10_0,
    };
    D3D_FEATURE_LEVEL fl_out;
    hr = D3D11CreateDevice(
        (IDXGIAdapter*)adapter,
        D3D_DRIVER_TYPE_UNKNOWN,
        NULL,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_SINGLETHREADED,
        fl_in, (UINT)(sizeof(fl_in)/sizeof(fl_in[0])),
        D3D11_SDK_VERSION,
        &g_d3d, &fl_out, &g_ctx);
    if (FAILED(hr)) goto fail;

    hr = adapter->lpVtbl->EnumOutputs(adapter, 0, &output);
    if (FAILED(hr)) goto fail;

    hr = output->lpVtbl->QueryInterface(output, &IID_IDXGIOutput1,
                                        (void**)&g_output1);
    if (FAILED(hr)) goto fail;

    DXGI_OUTPUT_DESC od;
    output->lpVtbl->GetDesc(output, &od);
    g_screen_w = od.DesktopCoordinates.right  - od.DesktopCoordinates.left;
    g_screen_h = od.DesktopCoordinates.bottom - od.DesktopCoordinates.top;
    if (g_screen_w <= 0 || g_screen_h <= 0) goto fail;

    hr = g_output1->lpVtbl->DuplicateOutput(g_output1, (IUnknown*)g_d3d, &g_dup);
    if (FAILED(hr)) goto fail;

    D3D11_TEXTURE2D_DESC sd;
    memset(&sd, 0, sizeof(sd));
    sd.Width          = 3;
    sd.Height         = 3;
    sd.MipLevels      = 1;
    sd.ArraySize      = 1;
    sd.Format         = DXGI_FORMAT_B8G8R8A8_UNORM;
    sd.SampleDesc.Count = 1;
    sd.Usage          = D3D11_USAGE_STAGING;
    sd.BindFlags      = 0;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    sd.MiscFlags      = 0;

    hr = g_d3d->lpVtbl->CreateTexture2D(g_d3d, &sd, NULL, &g_stage);
    if (FAILED(hr)) goto fail;

    compute_sample_points(g_screen_w, g_screen_h);

    output->lpVtbl->Release(output);
    adapter->lpVtbl->Release(adapter);
    factory->lpVtbl->Release(factory);
    return 0;

fail:
    if (output)  output->lpVtbl->Release(output);
    if (adapter) adapter->lpVtbl->Release(adapter);
    if (factory) factory->lpVtbl->Release(factory);
    dup_release_all();
    return -1;
}

static int dup_recreate(void) {
    dup_release_all();
    return dup_create();
}

/* Initialize DXGI Desktop Duplication + staging.
 *   min_bright_points: N-of-9 threshold (default 5 if <=0)
 *   bright_min:        per-channel floor for "bright" (default 150 if <=0)
 *   max_spread:        max(R,G,B)-min(R,G,B) for "achromatic" (default 25 if <=0)
 * Returns 0 on success, negative on failure.
 */
__declspec(dllexport)
int hot_visual_init(int32_t min_bright_points, int32_t bright_min,
                    int32_t max_spread)
{
    g_min_bright = (min_bright_points > 0) ? min_bright_points : 5;
    g_bright_min = (bright_min > 0) ? bright_min : 150;
    g_max_spread = (max_spread > 0) ? max_spread : 25;
    /* QueryPerformanceFrequency is also set in hot_init; re-run so the
     * visual path works standalone (e.g. tests that skip hot_init). */
    if (g_qpc_freq.QuadPart == 0) {
        QueryPerformanceFrequency(&g_qpc_freq);
    }
    return dup_create();
}

__declspec(dllexport)
int hot_visual_get_info(int32_t* out_w, int32_t* out_h,
                        int32_t* out_min_bright, int32_t* out_bright_min,
                        int32_t* out_max_spread)
{
    if (out_w)          *out_w = g_screen_w;
    if (out_h)          *out_h = g_screen_h;
    if (out_min_bright) *out_min_bright = g_min_bright;
    if (out_bright_min) *out_bright_min = g_bright_min;
    if (out_max_spread) *out_max_spread = g_max_spread;
    return 0;
}

/* One visual tick.
 *   already_dimmed: non-zero if MagSet(dark) is already active. We still
 *                   acquire/scan (cheap, lets us populate timing/debug),
 *                   but skip MagSet.
 *   out:            result struct
 * Returns 0 on success (no-frame is NOT an error),
 *         -1 not initialized, -2 access-lost (recreated), negative HRESULT otherwise.
 */
__declspec(dllexport)
int hot_visual_tick(int32_t already_dimmed, hot_visual_t* out)
{
    LARGE_INTEGER t0, t1, t2, t3, t4;
    QueryPerformanceCounter(&t0);
    memset(out, 0, sizeof(*out));

    if (!g_dup || !g_stage || !g_ctx || !g_d3d) {
        QueryPerformanceCounter(&t1);
        out->t_total_ns = qpc_diff_ns(t0, t1);
        return -1;
    }

    DXGI_OUTDUPL_FRAME_INFO fi;
    memset(&fi, 0, sizeof(fi));
    IDXGIResource* resource = NULL;
    HRESULT hr = g_dup->lpVtbl->AcquireNextFrame(g_dup, 0, &fi, &resource);
    QueryPerformanceCounter(&t1);
    out->t_acquire_ns = qpc_diff_ns(t0, t1);

    if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
        out->t_total_ns = out->t_acquire_ns;
        return 0;
    }
    if (hr == DXGI_ERROR_ACCESS_LOST) {
        if (resource) resource->lpVtbl->Release(resource);
        dup_recreate();
        out->t_total_ns = out->t_acquire_ns;
        return -2;
    }
    if (FAILED(hr) || resource == NULL) {
        if (resource) resource->lpVtbl->Release(resource);
        out->t_total_ns = out->t_acquire_ns;
        return -(int)(hr & 0x7FFFFFFF);
    }

    /* LastPresentTime == 0 means no presentation since last Acquire
     * (cursor update only). Texture may still be valid, but no new
     * pixel data. Skip to save work. */
    if (fi.LastPresentTime.QuadPart == 0) {
        resource->lpVtbl->Release(resource);
        g_dup->lpVtbl->ReleaseFrame(g_dup);
        QueryPerformanceCounter(&t4);
        out->t_total_ns = qpc_diff_ns(t0, t4);
        return 0;
    }

    out->had_frame = 1;
    g_visual_frame_n++;
    out->frame_num = (int32_t)(g_visual_frame_n & 0x7FFFFFFF);

    ID3D11Texture2D* frame_tex = NULL;
    hr = resource->lpVtbl->QueryInterface(resource, &IID_ID3D11Texture2D,
                                          (void**)&frame_tex);
    if (FAILED(hr) || frame_tex == NULL) {
        resource->lpVtbl->Release(resource);
        g_dup->lpVtbl->ReleaseFrame(g_dup);
        QueryPerformanceCounter(&t4);
        out->t_total_ns = qpc_diff_ns(t0, t4);
        return -(int)(hr & 0x7FFFFFFF);
    }

    /* 9x single-pixel CopySubresourceRegion from captured frame into
     * 3x3 staging. Pixel i maps to staging (i%3, i/3). */
    for (int i = 0; i < 9; i++) {
        D3D11_BOX box;
        box.left   = (UINT)g_sample_x[i];
        box.top    = (UINT)g_sample_y[i];
        box.front  = 0;
        box.right  = (UINT)g_sample_x[i] + 1;
        box.bottom = (UINT)g_sample_y[i] + 1;
        box.back   = 1;
        UINT dst_x = (UINT)(i % 3);
        UINT dst_y = (UINT)(i / 3);
        g_ctx->lpVtbl->CopySubresourceRegion(
            g_ctx,
            (ID3D11Resource*)g_stage, 0, dst_x, dst_y, 0,
            (ID3D11Resource*)frame_tex, 0, &box);
    }

    frame_tex->lpVtbl->Release(frame_tex);
    resource->lpVtbl->Release(resource);
    g_dup->lpVtbl->ReleaseFrame(g_dup);
    QueryPerformanceCounter(&t2);
    out->t_copy_ns = qpc_diff_ns(t1, t2);

    /* Map staging (blocks until copy commands finish) and count bright
     * achromatic pixels. Matches AHK: min(R,G,B) >= 150 && spread <= 25. */
    D3D11_MAPPED_SUBRESOURCE mapped;
    memset(&mapped, 0, sizeof(mapped));
    hr = g_ctx->lpVtbl->Map(g_ctx, (ID3D11Resource*)g_stage, 0,
                            D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) {
        QueryPerformanceCounter(&t4);
        out->t_total_ns = qpc_diff_ns(t0, t4);
        return -(int)(hr & 0x7FFFFFFF);
    }

    /* Check audio priming state. If a flashbang-like audio transient
     * was detected recently, loosen the visual thresholds so we catch
     * the ramp frame even from bright scenes. */
    LARGE_INTEGER t_now;
    QueryPerformanceCounter(&t_now);
    int primed = (g_audio_primed_until_qpc.QuadPart > t_now.QuadPart) ? 1 : 0;
    int rel_bmin   = primed ? g_audio_primed_bmin   : g_relaxed_bmin;
    int rel_spread = primed ? g_audio_primed_spread : g_relaxed_spread;
    int rel_min    = primed ? g_audio_primed_min    : g_rise_min;
    int delta_min  = primed ? g_audio_primed_delta_min    : g_delta_min;
    int delta_th   = primed ? g_audio_primed_delta_thresh : g_delta_thresh;
    /* When primed, we trust audio as a floor signal so we can catch the
     * very first bright frame of the flash. Lower the minimum-luma
     * gate too; otherwise a RGB ~180 ramp frame wouldn't satisfy the
     * sum>=450 requirement. */
    int cur_lum_floor = primed ? 380 : g_delta_min_cur_lum;

    int bright = 0;   /* strict: peak-flash gate */
    int relaxed = 0;  /* loose: ramp-frame gate */
    int delta_hits = 0;  /* pixels with luma jump >= threshold */
    int max_lum = 0;
    uint8_t cur_rgb[9 * 3];
    const uint8_t* base = (const uint8_t*)mapped.pData;
    for (int i = 0; i < 9; i++) {
        int dx = i % 3;
        int dy = i / 3;
        const uint8_t* px = base + (size_t)dy * mapped.RowPitch + (size_t)dx * 4;
        int B = px[0];
        int G = px[1];
        int R = px[2];
        cur_rgb[i*3+0] = (uint8_t)R;
        cur_rgb[i*3+1] = (uint8_t)G;
        cur_rgb[i*3+2] = (uint8_t)B;

        int mn = R < G ? (R < B ? R : B) : (G < B ? G : B);
        int mx = R > G ? (R > B ? R : B) : (G > B ? G : B);
        int spread = mx - mn;
        if (mn >= g_bright_min && spread <= g_max_spread) {
            bright++;
        }
        if (mn >= rel_bmin && spread <= rel_spread) {
            relaxed++;
        }

        /* Per-pixel luma delta: sum of per-channel absolute differences.
         * A flashbang jumps a pixel from scene-dark (~60-180 sum) to
         * saturated (~765 sum) in a single frame, delta ~400-700. A
         * wall or slow motion yields delta <100. */
        int lum = R + G + B;
        if (lum > max_lum) max_lum = lum;
        if (g_prev_rgb_valid) {
            int pR = g_prev_rgb[i*3+0];
            int pG = g_prev_rgb[i*3+1];
            int pB = g_prev_rgb[i*3+2];
            int dR = R > pR ? R - pR : pR - R;
            int dG = G > pG ? G - pG : pG - G;
            int dB = B > pB ? B - pB : pB - B;
            int luma_delta = dR + dG + dB;
            /* Only count rising deltas with sufficient brightness --
             * avoids firing on fade-from-bright-to-dark events. */
            if (luma_delta >= delta_th && lum >= cur_lum_floor) {
                delta_hits++;
            }
        }
    }
    g_ctx->lpVtbl->Unmap(g_ctx, (ID3D11Resource*)g_stage, 0);
    QueryPerformanceCounter(&t3);
    out->t_scan_ns = qpc_diff_ns(t2, t3);
    out->bright_count = bright;
    out->relaxed_count = relaxed;
    out->prev_relaxed = g_prev_relaxed;
    out->delta_count = delta_hits;
    out->max_luma = max_lum;
    out->audio_primed = primed;

    /* Five fire gates, any one can trigger:
     *   1 PEAK  : strict bright >= g_min_bright (7/9 RGB>=230) -- saturated.
     *   2 RISE  : relaxed >= rise_min AND prev_relaxed <= 1 -- dark->bright.
     *   3 DELTA : delta_hits >= delta_min -- fires from any baseline.
     *   4 PRIMED: audio primed + any of 2/3 with loose thresholds.
     *   5 RAMP  : strictly monotonic max_lum rise over 3 frames + >= 100
     *             total, with current >= 600. Catches the ramp ~33ms
     *             before PEAK saturates.
     * When primed, gates 2/3 use looser thresholds (set at top of block). */
    int peak_fire  = (bright >= g_min_bright);
    int rise_fire  = (relaxed >= rel_min && g_prev_relaxed <= g_rise_prev_max);
    int delta_fire = (g_prev_rgb_valid && delta_hits >= delta_min);

    int ramp_fire = 0;
    int ramp_total_rise = 0;
    if (g_maxlum_hist_fill >= 3) {
        int m0 = max_lum;
        int m1 = g_maxlum_hist[0];  /* N-1 */
        int m2 = g_maxlum_hist[1];  /* N-2 */
        int m3 = g_maxlum_hist[2];  /* N-3 */
        if (m0 > m1 && m1 > m2 && m2 > m3) {
            ramp_total_rise = m0 - m3;
            if (ramp_total_rise >= g_ramp_rise_min &&
                m0 >= g_ramp_cur_lum_min) {
                ramp_fire = 1;
            }
        }
    }
    out->ramp_rise = ramp_total_rise;

    /* Corroboration: every visual gate must also see the audio path primed
     * (flashbang tinnitus tone detected within the last ~prime_ms window).
     * This kills visual false positives from bright menus, Nuke daylight,
     * muzzle flashes, etc. — they have no 2200Hz sustained tone. */
    int fire_now = primed && (peak_fire || rise_fire || delta_fire || ramp_fire);

    if (fire_now && !already_dimmed) {
        /* Gamma ramp first (scanout path, lands ~1 frame faster than
         * MagSet), then MagSet for the full DWM-level dim. Even with
         * Win10's default GdiICMGammaRange clamp this still shaves ~16ms
         * off the visible flash. */
        if (g_gamma_hdc != NULL) {
            WORD ramp[256 * 3];
            gamma_build_ramp(ramp, g_gamma_min_bright);
            SetDeviceGammaRamp(g_gamma_hdc, ramp);
        }
        if (g_MagSet) {
            g_MagSet(&g_dark);
        }
        QueryPerformanceCounter(&t4);
        out->t_magset_ns = qpc_diff_ns(t3, t4);
        out->fired = 1;
        if (primed && (rise_fire || delta_fire || ramp_fire)) out->trigger_kind = 4;
        else if (peak_fire)                                    out->trigger_kind = 1;
        else if (ramp_fire)                                    out->trigger_kind = 5;
        else if (rise_fire)                                    out->trigger_kind = 2;
        else                                                   out->trigger_kind = 3;
    } else {
        t4 = t3;
        out->trigger_kind = 0;
    }

    /* Carry state forward. Reset prev_relaxed if fired so a second
     * flash during fade retriggers without waiting for a full dark
     * frame. Always update prev_rgb (needed for delta on next tick).
     * Rotate the max_lum history AFTER the gate check -- we compared
     * against the state from before this tick. On fire we flush the
     * history so stale-bright values can't keep re-firing post-dim. */
    g_prev_relaxed = out->fired ? 0 : relaxed;
    memcpy(g_prev_rgb, cur_rgb, sizeof(cur_rgb));
    g_prev_rgb_valid = 1;

    if (out->fired) {
        g_maxlum_hist[0] = 0;
        g_maxlum_hist[1] = 0;
        g_maxlum_hist[2] = 0;
        g_maxlum_hist_fill = 0;
    } else {
        g_maxlum_hist[2] = g_maxlum_hist[1];
        g_maxlum_hist[1] = g_maxlum_hist[0];
        g_maxlum_hist[0] = max_lum;
        if (g_maxlum_hist_fill < 3) g_maxlum_hist_fill++;
    }

    out->t_total_ns = qpc_diff_ns(t0, t4);
    return 0;
}

/* ======================================================================
 *   AUDIO HOT PATH: WASAPI loopback peak detection
 * ======================================================================
 *
 * WASAPI capture in loopback mode reads the mix stream being rendered
 * to the default speaker. That stream includes CS2's flashbang sound
 * effect, which produces a characteristic sharp transient (peak near
 * full scale) followed by a sustained high-frequency ringing tail.
 *
 * We don't FIRE MagSet from audio. We only set g_audio_primed_until_qpc
 * when a transient is detected. The visual path then uses looser
 * thresholds when primed. This is safe: through-wall flashes that have
 * sound but no visible effect will not dim the screen, because the
 * visual scan still sees no bright pixels.
 *
 * Detector (cheap, runs on main thread):
 *   1. Read all available samples via GetBuffer/ReleaseBuffer.
 *   2. Track max absolute sample (peak) and a short-window energy.
 *   3. If peak > 0.90 AND peak_rose_sharply: prime.
 *   4. Hysteresis: after priming, don't re-prime for 300ms to avoid
 *      chaining on the flash's own ringing tail.
 */

/* IIDs we need explicitly (INITGUID pulls the definitions). */
DEFINE_GUID(CLSID_MMDeviceEnumerator_local, 0xBCDE0395, 0xE52F, 0x467C,
            0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E);
DEFINE_GUID(IID_IMMDeviceEnumerator_local, 0xA95664D2, 0x9614, 0x4F35,
            0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6);
DEFINE_GUID(IID_IAudioClient_local, 0x1CB9AD4C, 0xDBFA, 0x4c32,
            0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2);
DEFINE_GUID(IID_IAudioCaptureClient_local, 0xC8ADBD64, 0xE71E, 0x48a0,
            0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17);

static IMMDeviceEnumerator*  g_audio_enum   = NULL;
static IMMDevice*            g_audio_device = NULL;
static IAudioClient*         g_audio_client = NULL;
static IAudioCaptureClient*  g_audio_cap    = NULL;
static WAVEFORMATEX*         g_audio_fmt    = NULL;
static int g_audio_channels  = 0;
static int g_audio_bits      = 0;
static int g_audio_is_float  = 0;
static UINT32 g_audio_sr     = 0;

/* Detector state. */
static float g_audio_peak_threshold = 0.90f;  /* absolute sample, 0..1 */
static float g_audio_rise_min       = 0.55f;  /* peak must rise by this */
static int   g_audio_rise_window_ms = 12;     /* within this ms */
static LARGE_INTEGER g_audio_last_prime_qpc;  /* for hysteresis */
static int   g_audio_reprime_cooldown_ms = 300;
static int   g_audio_prime_duration_ms   = 180;
/* Rolling ring buckets (one per ~12ms, 9 buckets = ~108ms window) so the
 * sharp bang peak can match a tone seen in a nearby batch without sticky
 * bugs that let unrelated prior bangs arm the gate. */
#define RING_BUCKETS 9
static float g_audio_ring_mag_bucket[RING_BUCKETS];
static float g_audio_ring_ratio_bucket[RING_BUCKETS];
static int   g_audio_ring_bucket_idx = 0;
static uint64_t g_audio_ring_last_rotate_ns = 0;
static const uint64_t g_audio_ring_bucket_ns = 12000000ULL;

/* Recent peak trace -- one bucket per ~3ms of audio. Used to detect
 * the rise: current bucket peak minus lowest peak in last N buckets. */
#define AUDIO_BUCKETS 8
static float g_audio_recent_peaks[AUDIO_BUCKETS];
static int   g_audio_bucket_idx = 0;
static uint64_t g_audio_last_bucket_qpc_ns = 0;
static uint64_t g_audio_bucket_period_ns = 3000000ULL;  /* 3ms */

/* Flashbang tinnitus detector. The CS2 flashbang sound, beneath the
 * initial bang, has a sustained narrowband tone around ~2200 Hz. Other
 * loud sounds (gunshots, HE grenades, explosions) are broadband and lack
 * that tone. We run a single-bin Goertzel per audio packet at the target
 * frequency; normalized magnitude / tonality ratio gives a flashbang-
 * specific signal.
 *
 * Goertzel (per block of N samples of a mono signal x[n]):
 *   coeff = 2 * cos(2*pi*target_hz / sample_rate)
 *   s_prev = s_prev2 = 0
 *   for each sample: s = x + coeff*s_prev - s_prev2; shift
 *   mag^2 = s_prev^2 + s_prev2^2 - coeff*s_prev*s_prev2
 *   mag   = sqrt(mag^2) / (N/2)   -- normalized so a pure sine at target
 *                                    frequency yields mag ~= amplitude.
 */
static float  g_audio_ring_target_hz = 2200.0f;
static float  g_audio_ring_coeff     = 0.0f;   /* 2*cos(w0) */
static float  g_audio_ring_min       = 0.0f;   /* min normalized mag to count */
static float  g_audio_ring_ratio_min = 0.0f;   /* min mag/rms tonality */

__declspec(dllexport)
int hot_audio_init(float peak_threshold, float rise_min,
                   int32_t prime_duration_ms)
{
    HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) return -1;

    if (g_qpc_freq.QuadPart == 0) QueryPerformanceFrequency(&g_qpc_freq);

    if (peak_threshold > 0.0f && peak_threshold <= 1.0f)
        g_audio_peak_threshold = peak_threshold;
    if (rise_min > 0.0f && rise_min <= 1.0f)
        g_audio_rise_min = rise_min;
    if (prime_duration_ms > 0)
        g_audio_prime_duration_ms = prime_duration_ms;

    hr = CoCreateInstance(&CLSID_MMDeviceEnumerator_local, NULL,
                          CLSCTX_ALL, &IID_IMMDeviceEnumerator_local,
                          (void**)&g_audio_enum);
    if (FAILED(hr)) return -2;

    hr = g_audio_enum->lpVtbl->GetDefaultAudioEndpoint(g_audio_enum,
            eRender, eConsole, &g_audio_device);
    if (FAILED(hr)) return -3;

    hr = g_audio_device->lpVtbl->Activate(g_audio_device,
            &IID_IAudioClient_local, CLSCTX_ALL, NULL,
            (void**)&g_audio_client);
    if (FAILED(hr)) return -4;

    hr = g_audio_client->lpVtbl->GetMixFormat(g_audio_client, &g_audio_fmt);
    if (FAILED(hr) || g_audio_fmt == NULL) return -5;

    g_audio_channels = g_audio_fmt->nChannels;
    g_audio_bits     = g_audio_fmt->wBitsPerSample;
    g_audio_sr       = g_audio_fmt->nSamplesPerSec;

    /* Detect float vs PCM by format tag. WAVE_FORMAT_EXTENSIBLE needs
     * SubFormat check; modern Windows mix is almost always float32. */
    if (g_audio_fmt->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) {
        g_audio_is_float = 1;
    } else if (g_audio_fmt->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        WAVEFORMATEXTENSIBLE* fex = (WAVEFORMATEXTENSIBLE*)g_audio_fmt;
        static const GUID sub_float = {0x00000003,0x0000,0x0010,
            {0x80,0x00,0x00,0xaa,0x00,0x38,0x9b,0x71}};
        g_audio_is_float = (memcmp(&fex->SubFormat, &sub_float,
                                   sizeof(GUID)) == 0) ? 1 : 0;
    } else {
        g_audio_is_float = 0;
    }

    /* Low-latency shared-mode loopback. 20ms buffer is plenty since
     * we tick continuously and drain the queue on each visit. */
    REFERENCE_TIME buffer_duration = 20 * 10000;  /* 20ms in 100ns units */
    hr = g_audio_client->lpVtbl->Initialize(g_audio_client,
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK,
            buffer_duration, 0, g_audio_fmt, NULL);
    if (FAILED(hr)) return -6;

    hr = g_audio_client->lpVtbl->GetService(g_audio_client,
            &IID_IAudioCaptureClient_local, (void**)&g_audio_cap);
    if (FAILED(hr)) return -7;

    hr = g_audio_client->lpVtbl->Start(g_audio_client);
    if (FAILED(hr)) return -8;

    g_audio_primed_until_qpc.QuadPart = 0;
    g_audio_last_prime_qpc.QuadPart = 0;
    for (int i = 0; i < AUDIO_BUCKETS; i++) g_audio_recent_peaks[i] = 0.0f;

    /* Precompute Goertzel coefficient now that sample rate is known. */
    if (g_audio_sr > 0 && g_audio_ring_target_hz > 0.0f) {
        double w0 = 2.0 * 3.14159265358979323846
                    * (double)g_audio_ring_target_hz / (double)g_audio_sr;
        g_audio_ring_coeff = (float)(2.0 * cos(w0));
    }

    return 0;
}

__declspec(dllexport)
int hot_audio_get_device_name(wchar_t* buf, int32_t buflen)
{
    if (!buf || buflen <= 0) return -1;
    buf[0] = L'\0';
    if (!g_audio_device) return -2;
    IPropertyStore* props = NULL;
    HRESULT hr = g_audio_device->lpVtbl->OpenPropertyStore(g_audio_device,
            STGM_READ, &props);
    if (FAILED(hr) || !props) return -3;
    PROPVARIANT pv;
    pv.vt = VT_EMPTY;
    hr = props->lpVtbl->GetValue(props, &PKEY_Device_FriendlyName, &pv);
    int rc = 0;
    if (SUCCEEDED(hr) && pv.vt == VT_LPWSTR && pv.pwszVal) {
        int i = 0;
        for (; i < buflen - 1 && pv.pwszVal[i]; i++) buf[i] = pv.pwszVal[i];
        buf[i] = L'\0';
    } else {
        rc = -4;
    }
    {
        typedef HRESULT (WINAPI *PPVC)(PROPVARIANT*);
        HMODULE h = GetModuleHandleW(L"ole32.dll");
        if (h) {
            PPVC f = (PPVC)GetProcAddress(h, "PropVariantClear");
            if (f) f(&pv);
        }
    }
    props->lpVtbl->Release(props);
    return rc;
}

__declspec(dllexport)
int hot_audio_configure_ring(float target_hz, float min_mag, float min_ratio)
{
    if (target_hz > 0.0f && target_hz < 20000.0f) {
        g_audio_ring_target_hz = target_hz;
        if (g_audio_sr > 0) {
            double w0 = 2.0 * 3.14159265358979323846
                        * (double)target_hz / (double)g_audio_sr;
            g_audio_ring_coeff = (float)(2.0 * cos(w0));
        }
    }
    if (min_mag >= 0.0f) g_audio_ring_min = min_mag;
    if (min_ratio >= 0.0f) g_audio_ring_ratio_min = min_ratio;
    return 0;
}

typedef struct {
    int32_t   had_samples;      /* 1 if we drained at least one packet */
    int32_t   primed;           /* 1 if we set the prime flag this tick */
    int32_t   frames_read;      /* raw sample frames read */
    float     peak;             /* max abs sample this tick */
    float     rise;             /* peak - min_recent_peak */
    float     ring_mag;         /* normalized Goertzel magnitude at target freq (flashbang tone) */
    float     ring_ratio;       /* ring_mag / max(rms, 1e-6) -- tonality */
    uint64_t  t_drain_ns;       /* enter -> after GetBuffer loop */
    uint64_t  t_total_ns;       /* enter -> return */
} hot_audio_t;

__declspec(dllexport)
int hot_audio_tick(hot_audio_t* out)
{
    LARGE_INTEGER t0, t1, t2;
    QueryPerformanceCounter(&t0);
    memset(out, 0, sizeof(*out));

    if (!g_audio_cap || !g_audio_client) {
        QueryPerformanceCounter(&t2);
        out->t_total_ns = qpc_diff_ns(t0, t2);
        return -1;
    }

    float peak = 0.0f;
    int frames_total = 0;

    /* Goertzel accumulator across all packets this tick, over mono
     * downmix. We collect sum_sq for RMS and run a single-bin Goertzel
     * at the configured target frequency. */
    float gz_s_prev = 0.0f, gz_s_prev2 = 0.0f;
    double sum_sq = 0.0;
    int   gz_frames = 0;  /* mono-sample count across the tick */
    const float gz_coeff = g_audio_ring_coeff;
    const int   nch = g_audio_channels > 0 ? g_audio_channels : 1;
    const float inv_nch = 1.0f / (float)nch;

    for (;;) {
        UINT32 packet_len = 0;
        HRESULT hr = g_audio_cap->lpVtbl->GetNextPacketSize(g_audio_cap,
                                                            &packet_len);
        if (FAILED(hr) || packet_len == 0) break;

        BYTE* data = NULL;
        UINT32 frames = 0;
        DWORD flags = 0;
        hr = g_audio_cap->lpVtbl->GetBuffer(g_audio_cap, &data, &frames,
                                            &flags, NULL, NULL);
        if (FAILED(hr) || frames == 0) {
            if (SUCCEEDED(hr)) {
                g_audio_cap->lpVtbl->ReleaseBuffer(g_audio_cap, frames);
            }
            break;
        }

        if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) == 0 && data != NULL) {
            out->had_samples = 1;
            frames_total += frames;

            if (g_audio_is_float && g_audio_bits == 32) {
                const float* s = (const float*)data;
                int total = (int)frames * nch;
                for (int i = 0; i < total; i++) {
                    float v = s[i];
                    if (v < 0.0f) v = -v;
                    if (v > peak) peak = v;
                }
                /* Mono downmix + Goertzel + sum_sq */
                for (UINT32 f = 0; f < frames; f++) {
                    float acc = 0.0f;
                    for (int c = 0; c < nch; c++) acc += s[f * nch + c];
                    acc *= inv_nch;
                    sum_sq += (double)acc * (double)acc;
                    float s_new = acc + gz_coeff * gz_s_prev - gz_s_prev2;
                    gz_s_prev2 = gz_s_prev;
                    gz_s_prev = s_new;
                    gz_frames++;
                }
            } else if (!g_audio_is_float && g_audio_bits == 16) {
                const int16_t* s = (const int16_t*)data;
                int total = (int)frames * nch;
                for (int i = 0; i < total; i++) {
                    int16_t v = s[i];
                    int av = (v < 0) ? -(int)v : (int)v;
                    float fv = (float)av / 32768.0f;
                    if (fv > peak) peak = fv;
                }
                for (UINT32 f = 0; f < frames; f++) {
                    float acc = 0.0f;
                    for (int c = 0; c < nch; c++) {
                        acc += (float)s[f * nch + c] / 32768.0f;
                    }
                    acc *= inv_nch;
                    sum_sq += (double)acc * (double)acc;
                    float s_new = acc + gz_coeff * gz_s_prev - gz_s_prev2;
                    gz_s_prev2 = gz_s_prev;
                    gz_s_prev = s_new;
                    gz_frames++;
                }
            }
        }

        g_audio_cap->lpVtbl->ReleaseBuffer(g_audio_cap, frames);
    }

    /* Finalize Goertzel magnitude. Normalize by N/2 so a pure sine at
     * the target bin yields mag ~= amplitude (0..1). RMS also 0..1. */
    float ring_mag = 0.0f;
    float ring_ratio = 0.0f;
    if (gz_frames > 8) {
        float mag_sq = gz_s_prev * gz_s_prev
                     + gz_s_prev2 * gz_s_prev2
                     - gz_coeff * gz_s_prev * gz_s_prev2;
        if (mag_sq < 0.0f) mag_sq = 0.0f;
        float mag = (float)sqrt((double)mag_sq);
        ring_mag = mag / ((float)gz_frames * 0.5f);
        float rms = (float)sqrt(sum_sq / (double)gz_frames);
        if (rms > 1e-6f) ring_ratio = ring_mag / rms;
    }
    out->ring_mag = ring_mag;
    out->ring_ratio = ring_ratio;

    QueryPerformanceCounter(&t1);
    out->t_drain_ns = qpc_diff_ns(t0, t1);
    out->frames_read = frames_total;
    out->peak = peak;

    /* Rotate bucket if enough real-time has passed. */
    uint64_t now_ns = (uint64_t)((t1.QuadPart * 1000000000ULL)
                                 / g_qpc_freq.QuadPart);
    if (now_ns - g_audio_last_bucket_qpc_ns >= g_audio_bucket_period_ns) {
        g_audio_bucket_idx = (g_audio_bucket_idx + 1) % AUDIO_BUCKETS;
        g_audio_recent_peaks[g_audio_bucket_idx] = peak;
        g_audio_last_bucket_qpc_ns = now_ns;
    } else {
        /* keep running max in the current bucket */
        if (peak > g_audio_recent_peaks[g_audio_bucket_idx]) {
            g_audio_recent_peaks[g_audio_bucket_idx] = peak;
        }
    }

    /* Compute rise: current peak minus the LOWEST peak among the
     * non-current buckets. A sharp transient shows a big gap. */
    float min_recent = 1.0f;
    for (int i = 0; i < AUDIO_BUCKETS; i++) {
        if (i == g_audio_bucket_idx) continue;
        if (g_audio_recent_peaks[i] < min_recent) {
            min_recent = g_audio_recent_peaks[i];
        }
    }
    float rise = peak - min_recent;
    out->rise = rise;

    /* Priming decision. Hysteresis: if we just primed, don't re-prime
     * within the cooldown window (the flash's own tail will keep the
     * peak high but that's not a new event). */
    uint64_t cooldown_ns = (uint64_t)g_audio_reprime_cooldown_ms * 1000000ULL;
    uint64_t since_last_prime = now_ns -
        (uint64_t)((g_audio_last_prime_qpc.QuadPart * 1000000000ULL)
                   / g_qpc_freq.QuadPart);
    int in_cooldown = (g_audio_last_prime_qpc.QuadPart != 0 &&
                       since_last_prime < cooldown_ns);

    /* Flashbang-specific gate: beyond "loud+sudden", require that the
     * packet contains a narrowband tone at the configured target freq
     * (flashbang tinnitus). ring_min / ring_ratio_min default to 0.0
     * (disabled); once Python sets them via hot_audio_configure_ring,
     * they filter out gunshots/explosions that lack the sustained ring. */
    /* Update ring rolling buckets. Rotate when a bucket interval has
     * elapsed so stale tone from seconds ago can never arm the gate. */
    if (now_ns - g_audio_ring_last_rotate_ns >= g_audio_ring_bucket_ns) {
        g_audio_ring_bucket_idx = (g_audio_ring_bucket_idx + 1) % RING_BUCKETS;
        g_audio_ring_mag_bucket[g_audio_ring_bucket_idx] = out->ring_mag;
        g_audio_ring_ratio_bucket[g_audio_ring_bucket_idx] = out->ring_ratio;
        g_audio_ring_last_rotate_ns = now_ns;
    } else {
        if (out->ring_mag > g_audio_ring_mag_bucket[g_audio_ring_bucket_idx])
            g_audio_ring_mag_bucket[g_audio_ring_bucket_idx] = out->ring_mag;
        if (out->ring_ratio > g_audio_ring_ratio_bucket[g_audio_ring_bucket_idx])
            g_audio_ring_ratio_bucket[g_audio_ring_bucket_idx] = out->ring_ratio;
    }
    float ring_mag_window = 0.0f;
    float ring_ratio_window = 0.0f;
    for (int i = 0; i < RING_BUCKETS; i++) {
        if (g_audio_ring_mag_bucket[i] > ring_mag_window)
            ring_mag_window = g_audio_ring_mag_bucket[i];
        if (g_audio_ring_ratio_bucket[i] > ring_ratio_window)
            ring_ratio_window = g_audio_ring_ratio_bucket[i];
    }
    int tone_ok = (g_audio_ring_min <= 0.0f ||
                   ring_mag_window >= g_audio_ring_min);
    int ratio_ok = (g_audio_ring_ratio_min <= 0.0f ||
                    ring_ratio_window >= g_audio_ring_ratio_min);

    if (peak >= g_audio_peak_threshold &&
        rise >= g_audio_rise_min &&
        tone_ok && ratio_ok &&
        !in_cooldown)
    {
        uint64_t prime_ns = (uint64_t)g_audio_prime_duration_ms * 1000000ULL;
        uint64_t until_ns = now_ns + prime_ns;
        LARGE_INTEGER until_qpc;
        until_qpc.QuadPart = (LONGLONG)((until_ns * g_qpc_freq.QuadPart)
                                         / 1000000000ULL);
        g_audio_primed_until_qpc = until_qpc;
        g_audio_last_prime_qpc = t1;
        out->primed = 1;
    }

    QueryPerformanceCounter(&t2);
    out->t_total_ns = qpc_diff_ns(t0, t2);
    return 0;
}

__declspec(dllexport)
int hot_audio_get_info(int32_t* out_sr, int32_t* out_ch, int32_t* out_bits,
                       int32_t* out_is_float)
{
    if (out_sr)       *out_sr = (int32_t)g_audio_sr;
    if (out_ch)       *out_ch = g_audio_channels;
    if (out_bits)     *out_bits = g_audio_bits;
    if (out_is_float) *out_is_float = g_audio_is_float;
    return 0;
}
