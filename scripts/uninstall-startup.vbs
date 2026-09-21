' ============================================================================
'  Quita el arranque automatico de Escapement (borra el acceso directo de Inicio).
'  Doble clic para desinstalar.
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

lnkPath = shell.SpecialFolders("Startup") & "\Escapement Voz.lnk"
If fso.FileExists(lnkPath) Then
    fso.DeleteFile lnkPath
    WScript.Echo "OK: Escapement ya no arrancara con Windows."
Else
    WScript.Echo "No estaba instalado (no hay acceso directo en Inicio)."
End If
