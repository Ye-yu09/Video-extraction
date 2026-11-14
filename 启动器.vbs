Option Explicit
Dim sh, root, py, healthUrl, pageUrl, i

root = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
py = root & "\.venv\Scripts\python.exe"
healthUrl = "http://127.0.0.1:8000/api/health"
pageUrl = "http://127.0.0.1:8000/"

Set sh = CreateObject("WScript.Shell")

Function IsRunning()
  On Error Resume Next
  Dim h
  Set h = CreateObject("WinHttp.WinHttpRequest.5.1")
  h.Open "GET", healthUrl, False
  h.Send
  If Err.Number = 0 Then
    IsRunning = (h.Status = 200)
  Else
    IsRunning = False
  End If
End Function

' 已在运行则直接开浏览器
If IsRunning() Then
  sh.Run pageUrl
  WScript.Quit
End If

sh.CurrentDirectory = root
sh.Environment("PROCESS")("HF_ENDPOINT") = "https://hf-mirror.com"
' 窗口模式 0 = 完全隐藏，不弹黑框
sh.Run """" & py & """ -m uvicorn app.main:app --host 127.0.0.1 --port 8000", 0, False

' 等待服务就绪（最多 30 秒）
For i = 1 To 30
  WScript.Sleep 1000
  If IsRunning() Then Exit For
Next

sh.Run pageUrl