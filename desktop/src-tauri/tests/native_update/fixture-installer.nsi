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
  ; Normalize ordinary and canonical extended-length spellings before the exact
  ; ownership comparison. The Rust fixture keeps the native spelling, while
  ; this also protects the gate if either side is canonicalized by Windows.
  StrCpy $1 $0
  StrCpy $2 $INSTDIR
  StrCpy $3 $1 4
  StrCmp $3 "\\?\" 0 +2
    StrCpy $1 $1 "" 4
  StrCpy $3 $2 4
  StrCmp $3 "\\?\" 0 +2
    StrCpy $2 $2 "" 4
  GetFullPathName $1 "$1"
  GetFullPathName $2 "$2"
  StrCmp $1 $2 allowed refused
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
