; Fixture installer ONLY: no registry, shortcuts, Runtime hook or elevation.
; The production Rust/PowerShell supervisor supplies /S /UPDATE /D=... .
Unicode true
Name "Avibe acceptance fixture"
OutFile "${OUTPUT}"
RequestExecutionLevel user
SilentInstall silent
AutoCloseWindow true
VIProductVersion "0.0.1.2"
VIAddVersionKey "ProductName" "Avibe"
VIAddVersionKey "ProductVersion" "0.0.1-rc.2"
VIAddVersionKey "FileVersion" "0.0.1-rc.2"
VIAddVersionKey "FileDescription" "Avibe native acceptance fixture installer"
VIAddVersionKey "LegalCopyright" "Fixture only"
Section
  ; Must agree with the observer-owned exact root. Never rely on NSIS's default.
  ReadEnvStr $0 "AVIBE_NATIVE_ACCEPTANCE_INSTALLATION"
  StrCmp $0 "" refused
  StrCmp $INSTDIR $0 allowed refused
  allowed:
    SetOutPath "$INSTDIR"
    File /oname=avibe-desktop.exe "${NEXT_BINARY}"
    IfErrors refused
    SetErrorLevel 0
    Quit
  refused:
    SetErrorLevel 1
    Quit
SectionEnd
