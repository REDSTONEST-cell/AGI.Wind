from pathlib import Path

path = Path(r"C:\Users\wind\Desktop\AGI.Wind\voice-ui\app.js")
text = path.read_text(encoding="utf-8")
text = text.replace('Number(bufferInput.value).toFixed(2) + "s"', 'Number(bufferInput.value).toFixed(2) + "秒"')
text = text.replace('"流式生成 - " + s.toFixed(2) + "s"', '"流式生成 - " + s.toFixed(2) + "秒"')
text = text.replace('"生成中 - " + (b / 2 / sr).toFixed(2) + "s"', '"生成中 - " + (b / 2 / sr).toFixed(2) + "秒"')
text = text.replace('"接收中 - " + (b / 2 / sr).toFixed(2) + "s"', '"接收中 - " + (b / 2 / sr).toFixed(2) + "秒"')
text = text.replace('shown + " of " + voices.length', 'shown + " / " + voices.length')
path.write_text(text, encoding="utf-8")
print("done")
