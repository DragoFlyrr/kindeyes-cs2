#NoEnv
#Persistent
#SingleInstance Force
SetBatchLines, -1

; Define path to settings file
IniFile := A_ScriptDir . "\settings.ini"
if !FileExist(IniFile) {
    IniWrite, }, %IniFile%, Settings, HotkeyToggle
    IniWrite, ., %IniFile%, Settings, HotkeyEyelids
    IniWrite, 200, %IniFile%, Settings, Transparency
    IniWrite, 50, %IniFile%, Settings, CheckInterval
    IniWrite, 10, %IniFile%, Settings, FadingEvery
    IniWrite, 0x000000, %IniFile%, Settings, BkColor
}

Winget, id, id, A
WinSet, ExStyle, ^0x80,  ahk_id %id% ; 0x80 is WS_EX_TOOLWINDOW


; --- Default Settings ---
TargetColor := 0xFFFFFF
ColorTolerance := 10
CoverageThreshold := 100
IniRead, HotkeyToggle, %IniFile%, Settings, HotkeyToggle, }
IniRead, HotkeyEyelids, %IniFile%, Settings, HotkeyEyelids, .
IniRead, Transparency, %IniFile%, Settings, Transparency, 200
IniRead, CheckInterval, %IniFile%, Settings, CheckInterval, 50
IniRead, FadingEvery, %IniFile%, Settings, FadingEvery, 10
IniRead, BkColor, %IniFile%, Settings, BkColor, 0x000000, 
Enabled := true
global EyelidsClosed = 0
global valueFading := 200

global appVersion := "v2.53"
global AutoGuiW, EyelidColor, Bottom_OffsetX, Bottom_OffsetY, Bottom_Screen, Bottom_Win, DisplaySec
global FixedX, FixedY, FontColor, FontName, FontSize, FontStyle, GuiHeight, GuiPosition, GuiWidth
global SettingsGuiIsOpen, ShowModifierKeyCount, ShowMouseButton, ShowSingleModifierKey
global ShowStickyModKeyCount, Top_OffsetX, Top_OffsetY, Top_Screen, Top_Win, hGui_OSD, hGUI_s



; Read saved settings or set defaults







CreateTrayMenu()


CreateTrayMenu() {
	Menu, Tray, NoStandard
	Menu, Tray, Add, Settings, ShowSettingsGUI
	Menu, Tray, Add
	Menu, Tray, Add, Exit, _ExitApp
	Menu, Tray, Default, Settings
}

_ExitApp() {
    SaveSettings()
    ExitApp
}



sGuiAddTitleText(text) {
	Gui, s:Font, s16
	Gui, s:Add, Text, xm y+20, %text%
	Gui, s:Font, s12
}

ShowSettingsGUI() {
	global HotkeyToggle
	global HotkeyEyelids
	global Transparency
	global CheckInterval
	global FadingEvery
		global NewHotkey
		global FadingEveryN
		global TransNVal
		global CheckNVal
		global NewEyelidToggle
		global HotkeyEyelids
		
	SettingsGuiIsOpen := true

	Gui, s:Destroy
	Gui, s:+HWNDhGUI_s
	Gui, s:Font, s12

	Gui, s:Add, Button, x+82 yp+2 gResetDefaults, Reset to Defaults

	Gui, s:Add, Text, xm yp+34, ____________________________________
	Gui, s:Add, Text, xm yp+20, To start on boot, copy script to this folder:
	Gui, s:Add, Button, xm+73 yp+20 gOpenStartupFolder, Open Startup Folder
	Gui, s:Add, Text, xm yp+34, ____________________________________
	
	Gui, s:Add, Text, xm y+1, Enable/Disable script:
	Gui, s:Add, Hotkey, x+10 w100 vNewHotkey gHotkeyChanged, %HotkeyToggle%

	Gui, s:Add, Text, xm yp+28, ____________________________________

	Gui, s:Add, Text, xm y+1, Eyelid Key:
	Gui, s:Add, Hotkey, x+10 w100 vNewEyelidToggle gEyelidToggleChanged, %HotkeyEyelids%

	Gui, s:Add, Text, xm yp+28, ____________________________________

	Gui, s:Add, Text, xm y+1, Transparency:
	Gui, s:Add, Slider, xm+10 w300 vTransN Range50-255 ToolTip gUpdateTransVal, %Transparency%

	Gui, s:Add, Text, xm yp+25, ____________________________________

	Gui, s:Add, Text, xm y+1, Increase this value only if you're losing FPS
	Gui, s:Add, Text, xm y+1, Check Every (milliseconds):
	Gui, s:Add, Slider, xm+10 w300 vCheckN Range1-500 ToolTip gUpdateCheckInterval, %CheckInterval%

	Gui, s:Add, Text, xm yp+25, ____________________________________

	Gui, s:Add, Text, xm y+1, Fading (How fast it disappears): 
	Gui, s:Add, Slider, xm+10 w300 vFadingEveryN Range5-200 ToolTip gUpdateFadingEvery, %FadingEvery%

	Gui, s:Add, Text, xm yp+25, ____________________________________

	Gui, s:Add, Button, xm+82 y+1 gChangeBkColor, Change Overlay Color

	Gui, s:Add, Text, xm yp+34, ____________________________________
	Gui, s:Add, Text, xm y+5, >-------- Created by Brian Vuksanovich --------<
	Gui, s:Add, Text, xm y+10, Report bugs by commenting on
	Gui, s:Add, Button, xm+220 yp-7 gOpenWebsite, this video
	Gui, s:Add, Text, xm+40 yp+40, and
	Gui, s:Add, Button, x+6 yp-7 gOpenSubscribe, Subscribe to my channel!





	if (GuiPosition = "Fixed Position")
		OSD_EnableDrag()
	Gui, s:Show,, Settings - FlashbangColorChanger



	_CheckValues:
		Loop, Parse, Bottom_OffsetX,Bottom_OffsetY,Top_OffsetX,Top_OffsetY,FixedX,FixedY
		{
			if (%A_LoopField% = "") {
				%A_LoopField% := 0
			}
		}
	return

	_AutoGuiW:
		; GuiControlGet, AutoGuiW, s:
		Gui, Submit, NoHide
		GuiControl, % "s:Enable" !AutoGuiW, GuiWidth
		GuiControl, % "s:Enable" !AutoGuiW, GuiWUD
		GuiControl, 1:+Redraw, HotkeyText
	return

	UpdateGuiPosition:
		Gui, Submit, NoHide
		if (NewHotkey != "") {
			Hotkey, %HotkeyToggle%, ToggleScript, Off  ; remove old hotkey
			HotkeyToggle := NewHotkey
			Hotkey, %HotkeyToggle%, ToggleScript, On   ; set new hotkey
		}

		if (GuiPosition = "Fixed Position")
			OSD_EnableDrag()
		else
			OSD_DisableDrag()
	return


	OpenWebsite:
		Run, https://www.youtube.com/watch?v=TJouE879EUQ
    return

	OpenSubscribe:
		Run, https://www.youtube.com/@brian-vuksanovich?sub_confirmation=1
    return
	
	UpdateTransVal:
		global TransNVal
		global TransN
		GuiControlGet, TransN
		GuiControl,, TransNVal, % TransN
		Transparency = %TransN%
		SaveSettings()
	return

	UpdateCheckInterval:
		global CheckNVal
		global CheckN
		GuiControlGet, CheckN
		GuiControl,, CheckNVal, % CheckN
		CheckInterval = %CheckN%
		SetTimer, CheckForColor, Off     ; Stop the existing timer
		SetTimer, CheckForColor, %CheckInterval%
		SaveSettings()
	return
	
	UpdateFadingEvery:
		global FadingEvery
		GuiControlGet, FadingEveryN
		FadingEvery := FadingEveryN
		SaveSettings()
	return

	ChangeBkColor:
		global BkColor
		newColor := BkColor
		if Select_Color(hGUI_s, newColor) {
			Gui, 1:Color, %newColor%
			BkColor := newColor
			SaveSettings()
		}
	return
	
	ChangeEyelidColor:
		newColor := EyelidColor
		if Select_Color(hGUI_s, newColor) {
			Gui, 1:Color, %newColor%
			EyelidColor := newColor
			SaveSettings()
		}
	return
	
	UpdateFontSize:
		GuiControlGet, FontSize
		Gui, 1:Font, s%FontSize%
		GuiControl, 1:Font, HotkeyText
	return
	
	
	
	; --- Hotkey Change Handler ---
	HotkeyChanged:
		Gui, s:Submit, NoHide  ; Capture the new hotkey entered by the user

		; Check if the entered hotkey is valid and handle it gracefully
		if (NewHotkey == "") {
			; If nothing entered, just skip
			return
		}

		; Try applying the new hotkey, but if it fails, just ignore it
		Try {
			; Unbind the old hotkey (if it exists)
			if (HotkeyToggle != "") {
				Hotkey, %HotkeyToggle%, ToggleScript, Off
			}

			; Bind the new hotkey
			Hotkey, %NewHotkey%, ToggleScript, On
			; If we reach here, it means the key was valid, so we update the hotkey
			HotkeyToggle := NewHotkey
		}
		Catch {
			; If an error occurs (e.g., invalid hotkey), just silently ignore it
		}

			; Focus on the Transparency slider (or any other control you choose)
			GuiControl, Focus, TransN  ; Move focus to the Transparency slider
		SaveSettings()
	return



	; --- Apply New Hotkey ---
	ApplyNewHotkey:
		Gui, s:Submit, NoHide  ; Submit the GUI input

			; Unbind the old hotkey
			if (HotkeyToggle != "") {
				Hotkey, %HotkeyToggle%, ToggleScript, Off
			}
			; Set the new hotkey
			HotkeyToggle := NewHotkey
			; Bind the new hotkey
			Hotkey, %HotkeyToggle%, ToggleScript, On
	return
	
	
	
	; --- EyelidToggleChanged Change Handler ---
		EyelidToggleChanged:
			global NewEyelidToggle
			global HotkeyEyelids
			Gui, s:Submit, NoHide  ; Capture the new hotkey entered by the user

			; Check if the entered hotkey is valid and handle it gracefully
			if (NewEyelidToggle == "") {
				; If nothing entered, just skip
				
				return
			}

			; Try applying the new hotkey, but if it fails, just ignore it
			Try {
				; Unbind the old hotkey (if it exists)
				if (HotkeyEyelids != "") {
					Hotkey, %HotkeyEyelids%, ToggleOverlay, Off
				}

				; Bind the new hotkey
				Hotkey, %NewEyelidToggle%, ToggleOverlay, On
				; If we reach here, it means the key was valid, so we update the hotkey
				HotkeyEyelids := NewEyelidToggle
			}
			Catch {
				; If an error occurs (e.g., invalid hotkey), just silently ignore it
			}

			; Focus on the Transparency slider (or any other control you choose)
			GuiControl, Focus, TransN  ; Move focus to the Transparency slider
		SaveSettings()
	return
	
		
	ResetDefaults:
		; Make sure the variables are global
		global HotkeyToggle, HotkeyEyelids, Transparency, CheckInterval, FadingEvery, BkColor
		
		; Unbind hotkeys
		Hotkey, %HotkeyToggle%, ToggleScript, Off
		Hotkey, %HotkeyEyelids%, ToggleOverlay, Off
		
		; Load them into your script variables again
		HotkeyToggle := "}"  ; Set the default value for HotkeyToggle
		HotkeyEyelids := "."  ; Set the default value for HotkeyEyelids
		Transparency := 200  ; Set the default value for Transparency
		CheckInterval := 50  ; Set the default value for CheckInterval
		FadingEvery := 10    ; Set the default value for FadingEvery
		BkColor := 0x000000  ; Set the default value for BkColor
		
		; Update GUI controls with new values
		GuiControl,, NewHotkey, %HotkeyToggle%
		GuiControl,, NewEyelidToggle, %HotkeyEyelids%
		GuiControl,, TransN, %Transparency%
		GuiControl,, CheckN, %CheckInterval%
		GuiControl,, FadingEveryN, %FadingEvery%
		GuiControl,, ColorInput, %BkColor%
		
		; Rebind hotkeys with default values
		Hotkey, %HotkeyToggle%, ToggleScript, On
		Hotkey, %HotkeyEyelids%, ToggleOverlay, On
		
		; Save the reset settings to the INI file
		SaveSettings()

	return
	
}


WM_LBUTTONDOWN(wParam, lParam, msg, hwnd) {
	static hCursor := DllCall("LoadCursor", "Uint", 0, "Int", 32646, "Ptr") ; SizeAll = 32646

	if (hwnd = hGui_OSD) {
		PostMessage, 0xA1, 2
		DllCall("SetCursor", "ptr", hCursor)
	}
}

WM_MOVE(wParam, lParam, msg, hwnd) {
	if (hwnd = hGui_OSD) && GetKeyState("LButton", "P")
	{
		GuiControl, s:, FixedX, % lParam << 48 >> 48
		GuiControl, s:, FixedY, % lParam << 32 >> 48
	}
}

OSD_EnableDrag() {
	OnMessage(0x0201, "WM_LBUTTONDOWN")
	OnMessage(0x0003, "WM_MOVE")
	Gui, 1:-E0x20
}

OSD_DisableDrag() {
	OnMessage(0x0201, "")
	OnMessage(0x0003, "")
	Gui, 1:+E0x20
}







;-------------------------------------------------------------------------------
Select_Color(hGui, ByRef Color) { ; using comdlg32.dll
;-------------------------------------------------------------------------------

    ; CHOOSECOLOR structure expects text color in BGR format
    BGR := convert_Color(Color)

    ; unused, but a valid pointer to the structure
    VarSetCapacity(CUSTOM, 64, 0)


    ;-----------------------------------
    ; CHOOSECOLOR structure
    ;-----------------------------------

    If (A_PtrSize = 8) { ; 64 bit
        VarSetCapacity(CHOOSECOLOR, 72, 0)
        NumPut(     72, CHOOSECOLOR,  0) ; StructSize
        NumPut(   hGui, CHOOSECOLOR,  8) ; hwndOwner
        NumPut(    BGR, CHOOSECOLOR, 24) ; bgrColor
        NumPut(&CUSTOM, CHOOSECOLOR, 32) ; lpCustColors
        NumPut(  0x103, CHOOSECOLOR, 40) ; Flags
    }

    Else { ; 32 bit
        VarSetCapacity(CHOOSECOLOR, 36, 0)
        NumPut(     36, CHOOSECOLOR,  0) ; StructSize
        NumPut(   hGui, CHOOSECOLOR,  4) ; hwndOwner
        NumPut(    BGR, CHOOSECOLOR, 12) ; bgrColor
        NumPut(&CUSTOM, CHOOSECOLOR, 16) ; lpCustColors
        NumPut(  0x103, CHOOSECOLOR, 20) ; Flags
    }


    ;-----------------------------------
    ; call ChooseColorA function
    ;-----------------------------------

    If Not DllCall("comdlg32\ChooseColorA", "UInt", &CHOOSECOLOR)
        Return, False


    ;-----------------------------------
    ; result to return
    ;-----------------------------------

    ; chosen color
    RGB := convert_Color(NumGet(CHOOSECOLOR, A_PtrSize = 8 ? 24 : 12, "UInt"))
    Color := SubStr("0x00000", 1, 10 - StrLen(RGB)) SubStr(RGB, 3)
    Return, True
}



;-------------------------------------------------------------------------------
convert_Color(Color) { ; convert RGB <--> BGR
;-------------------------------------------------------------------------------
    $_FormatInteger := A_FormatInteger
    SetFormat, Integer, Hex
    Result := (Color & 0xFF) << 16 | Color & 0xFF00 | (Color >> 16) & 0xFF
    SetFormat, Integer, % $_FormatInteger
    Return, Result
}



















; Set initial hotkey
Hotkey, %HotkeyToggle%, ToggleScript, On
Hotkey, %HotkeyEyelids%, ToggleOverlay, On

; --- Set Timer ---
SetTimer, CheckForColor, %CheckInterval%

return








; --- FUNCTIONS ---

CheckForColor:
if (!Enabled)
    return

FoundPixels := 0
TotalSamples := 0
Step := 500

Loop, % (A_ScreenWidth // Step) {
    x := (A_Index - 1) * Step
    Loop, % (A_ScreenHeight // Step) {
        y := (A_Index - 1) * Step
        PixelGetColor, pixelColor, %x%, %y%, RGB
        if (Abs((pixelColor & 0xFF) - (TargetColor & 0xFF)) <= ColorTolerance
         && Abs(((pixelColor >> 8) & 0xFF) - ((TargetColor >> 8) & 0xFF)) <= ColorTolerance
         && Abs(((pixelColor >> 16) & 0xFF) - ((TargetColor >> 16) & 0xFF)) <= ColorTolerance)
        {
            FoundPixels++
        }
        TotalSamples++
    }
}

Coverage := (FoundPixels / TotalSamples) * 100

if (Coverage >= CoverageThreshold or EyelidsClosed = 1)
    ShowOverlay(BkColor)
return


ShowOverlay(color := "Black") {
    global Transparency
	if (EyelidsClosed = 0){
		Gui, Destroy
	}
    WinGet, activeWindow, ID, A  ; Save currently active window
	Gui, +AlwaysOnTop +ToolWindow -Caption +E0x80020 +LastFound -DPIScale
    Gui, Color, %color%

    Gui, Show, x1 y0 w%A_ScreenWidth% h%A_ScreenHeight% NoActivate, Overlay
    WinSet, Transparent, %Transparency%, Overlay
    WinActivate, ahk_id %activeWindow%
    SetTimer, FadeOutOverlay, 1
}



FadeOutOverlay:
	global FadingEvery
    FoundPixel := 0

            PixelGetColor, pixelColor, 1, 1, RGB
            if (Abs((pixelColor & 0xFF) - (TargetColor & 0xFF)) <= ColorTolerance
             && Abs(((pixelColor >> 8) & 0xFF) - ((TargetColor >> 8) & 0xFF)) <= ColorTolerance
             && Abs(((pixelColor >> 16) & 0xFF) - ((TargetColor >> 16) & 0xFF)) <= ColorTolerance)
            {
                FoundPixels++
            }

    if (FoundPixels > 0)
    {
;		MsgBox, This is a message.
        valueFading := 200
    } else if (EyelidsClosed = 1){
;		Sleep 1000
        valueFading := 200
		EyelidsClosed = 0
    } else {
			valueFading -= FadingEvery
			if (valueFading <= 0)
			{
				SetTimer, FadeOutOverlay, Off
				Gui, Destroy
				return	
			}
    }
return




; --- Hotkey Close Eyelids ---

ToggleOverlay:
    if (EyelidsClosed = 0) {
		EyelidsClosed = 1
    } else {
		EyelidsClosed = 0
    }
return



; --- MENU HANDLERS ---

ToggleScript:
Enabled := !Enabled
    if (Enabled) {
        DllCall("ShowCursor", "Int", false)
        cursorHidden := true
    } else {
		DllCall("ShowCursor", "Int", true)
    }
Gui, Destroy
return



OnExit("SaveSettings")

SaveSettings() {
    global IniFile, HotkeyToggle, HotkeyEyelids, Transparency, CheckInterval, FadingEvery, BkColor
    IniWrite, %HotkeyToggle%, %IniFile%, Settings, HotkeyToggle
    IniWrite, %HotkeyEyelids%, %IniFile%, Settings, HotkeyEyelids
    IniWrite, %Transparency%, %IniFile%, Settings, Transparency
    IniWrite, %CheckInterval%, %IniFile%, Settings, CheckInterval
    IniWrite, %FadingEvery%, %IniFile%, Settings, FadingEvery
    IniWrite, %BkColor%, %IniFile%, Settings, BkColor
}

OpenStartupFolder:
    Run, shell:startup
return



ExitScript:
ExitApp
