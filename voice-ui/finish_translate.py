from pathlib import Path

path = Path(r"C:\Users\wind\Desktop\AGI.Wind\voice-ui\app.js")
text = path.read_text(encoding="utf-8")
text = text.replace('v.seconds.toFixed(1) + "s"', 'v.seconds.toFixed(1) + "秒"')
text = text.replace('body.seconds.toFixed(2) + "s"', 'body.seconds.toFixed(2) + "秒"')
text = text.replace('"ERROR " + res.status', '"错误 " + res.status')
text = text.replace('shown + " of " + voices.length', 'shown + " / " + voices.length')
text = text.replace('(p.samples / sr).toFixed(2) + "s"', '(p.samples / sr).toFixed(2) + "秒"')
text = text.replace('v.seconds.toFixed(1) + "s)"', 'v.seconds.toFixed(1) + "秒)"')
path.write_text(text, encoding="utf-8")
print("done")
