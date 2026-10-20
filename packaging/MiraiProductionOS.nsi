Unicode True
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!include "MUI2.nsh"

!ifndef PRODUCT_VERSION
  !define PRODUCT_VERSION "0.0.0-dev"
!endif
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef OUTPUT_DIR
  !define OUTPUT_DIR "."
!endif

!define PRODUCT_NAME "Mirai Production OS"
!define PRODUCT_PUBLISHER "YOROKOBI"
!define PRODUCT_EXE "MiraiProductionOS.exe"

Name "${PRODUCT_NAME}"
OutFile "${OUTPUT_DIR}\MiraiProductionOS-${PRODUCT_VERSION}-Setup.exe"
InstallDir "$PROGRAMFILES64\Mirai Production OS"
InstallDirRegKey HKLM "Software\YOROKOBI\MiraiProductionOS" "InstallDir"
BrandingText "YOROKOBI"

VIProductVersion "0.13.0.0"
VIAddVersionKey /LANG=1041 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1041 "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=1041 "FileDescription" "${PRODUCT_NAME} Installer"
VIAddVersionKey /LANG=1041 "ProductVersion" "${PRODUCT_VERSION}"

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${PRODUCT_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Mirai Production OS を起動"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Japanese"
!insertmacro MUI_LANGUAGE "English"

Section "Mirai Production OS" SEC_MAIN
  SetOutPath "$INSTDIR"
  File /r "${SOURCE_DIR}\*"

  WriteRegStr HKLM "Software\YOROKOBI\MiraiProductionOS" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS" "NoRepair" 1

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\Mirai Production OS"
  CreateShortcut "$SMPROGRAMS\Mirai Production OS\Mirai Production OS.lnk" "$INSTDIR\${PRODUCT_EXE}"
  CreateShortcut "$DESKTOP\Mirai Production OS.lnk" "$INSTDIR\${PRODUCT_EXE}"
SectionEnd

Section "Uninstall"
  ; ユーザーのDB/動画/tokenは %LOCALAPPDATA%\YOROKOBI\MiraiProductionOS にあるため削除しない。
  Delete "$DESKTOP\Mirai Production OS.lnk"
  Delete "$SMPROGRAMS\Mirai Production OS\Mirai Production OS.lnk"
  RMDir "$SMPROGRAMS\Mirai Production OS"

  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MiraiProductionOS"
  DeleteRegKey HKLM "Software\YOROKOBI\MiraiProductionOS"

  RMDir /r "$INSTDIR"
SectionEnd
