' Ярлык для Рабочего стола: создаёт "Token Tracker.lnk" (панель управления) в папке проекта
Dim ws, lnk, workdir
workdir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
Set ws = CreateObject("WScript.Shell")
Set lnk = ws.CreateShortcut(workdir & "\Token Tracker.lnk")
lnk.TargetPath = workdir & "\launcher.pyw"
lnk.WorkingDirectory = workdir
lnk.Description = "Token Tracker — панель управления"
lnk.Save
If ws.FileExists(workdir & "\Meme Tracker.lnk") Then ws.DeleteFile workdir & "\Meme Tracker.lnk"
ws.Popup "Ярлык «Token Tracker» создан в папке проекта." & vbCrLf & _
         "Перетащите его на Рабочий стол.", 6, "Token Tracker", 64
