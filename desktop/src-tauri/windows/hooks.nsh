; Tauri includes hooks before MUI_LANGUAGE, so use the stable Windows language
; IDs here. Both languages are enabled in tauri.conf.json.
!include LogicLib.nsh
LangString AvibeRuntimePreparing 1033 "Preparing Avibe Runtime..."
LangString AvibeRuntimePreparing 2052 "正在准备 Avibe Runtime..."
LangString AvibeRuntimePrepareFailed 1033 "Avibe Runtime could not be prepared. Please run the installer again."
LangString AvibeRuntimePrepareFailed 2052 "无法准备 Avibe Runtime。请重新运行安装程序。"

!macro NSIS_HOOK_POSTINSTALL
  Push $0
  DetailPrint "$(AvibeRuntimePreparing)"
  ; Keep the executable's path out of the progress text.
  SetDetailsPrint none
  ClearErrors
  ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --prepare-runtime' $0
  SetDetailsPrint lastused
  ${If} ${Errors}
    StrCpy $0 1
  ${EndIf}
  ${If} $0 != 0
    DetailPrint "$(AvibeRuntimePrepareFailed)"
    ${IfNot} ${Silent}
    ${AndIf} $UpdateMode != 1
    ${AndIf} $PassiveMode != 1
      MessageBox MB_OK|MB_ICONSTOP "$(AvibeRuntimePrepareFailed)"
    ${EndIf}
    Pop $0
    SetErrorLevel 1
    Abort
  ${EndIf}
  Pop $0
!macroend
