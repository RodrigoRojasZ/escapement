' ============================================================================
'  Launcher de Escapement en modo VOZ, SIN ventana de consola (residente/background).
'
'  - Arranca 'pythonw -m agent.cli --voz' (pythonw = Python sin consola).
'  - El hotkey F2 (push-to-talk) es GLOBAL: funciona desde cualquier ventana.
'  - El feedback es hablado (TTS de Kokoro); no hay consola visible.
'
'  USO:
'    * Doble clic en este archivo para arrancar Escapement en segundo plano.
'    * Arranque automatico con Windows: pon un acceso directo a este .vbs en
'      la carpeta de inicio  ->  Win+R  ->  shell:startup  ->  pega el acceso directo.
'    * Para detenerlo: Administrador de tareas -> finalizar 'pythonw.exe'.
'
'  No usa 'uv run' a proposito: pythonw del venv arranca directo, sin sincronizar
'  (mas rapido y no dispara el lock del .exe).
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Este .vbs vive en <repo>\scripts\ ; el venv esta en <repo>\.venv
repoDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonw = repoDir & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonw) Then
    MsgBox "No encuentro el entorno de Escapement:" & vbCrLf & pythonw & vbCrLf & _
           "Corre 'uv sync' en " & repoDir & " primero.", vbCritical, "Escapement"
    WScript.Quit 1
End If

shell.CurrentDirectory = repoDir
' 0 = ventana oculta ; False = no esperar (el proceso queda residente)
shell.Run """" & pythonw & """ -m agent.cli --voz", 0, False
