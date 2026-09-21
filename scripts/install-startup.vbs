' ============================================================================
'  Instala el ARRANQUE AUTOMATICO de Escapement (modo voz) con Windows.
'  Crea un acceso directo a 'escapement-voz.vbs' en la carpeta de Inicio.
'  Doble clic para instalar.  Para desinstalar: uninstall-startup.vbs
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = scriptDir & "\escapement-voz.vbs"
If Not fso.FileExists(target) Then
    WScript.Echo "No encuentro el launcher: " & target
    WScript.Quit 1
End If

startup = shell.SpecialFolders("Startup")
lnkPath = startup & "\Escapement Voz.lnk"
Set lnk = shell.CreateShortcut(lnkPath)
lnk.TargetPath = target
lnk.WorkingDirectory = fso.GetParentFolderName(scriptDir)
lnk.Description = "Escapement en modo voz (residente, hotkey F2)"
lnk.Save

WScript.Echo "OK: Escapement arrancara con Windows." & vbCrLf & "Acceso directo: " & lnkPath
