# Writing a jgmDeb app
A package is a `.tar.gz` of a folder with:

```
manifest.json   {"id": "myapp", "name": "My App", "version": "1.0.0", "author": "you", "description": "...", "color": [r,g,b], "entry": "app.py", "factory": "make"}
app.py          defines make() -> an App instance
icon.png        optional 256x256 icon (shaped to the user's corner-radius setting)
```

The loader runs `app.py` with a global `shell` (the jgmDeb UI program). Use it for everything:

```python
class Hello(shell.App):
    title = 'Hello'; left = ''; right = 'Back'; tick = 1          # tick: redraw every N seconds (0 = only on key press)
    def draw(self, d, img):                                        # d: PIL ImageDraw, 240x320; colours are shell.FG, shell.DIM, shell.ACC, shell.WARN, shell.BG
        shell.ctext(d, shell.W / 2, 100, 'Hello', shell.font(24, True), shell.ACC)
    def key(self, k):                                              # '0'-'9' '*' '#' UP DOWN LEFT RIGHT OK LSK RSK CALL END
        if k in ('RSK', 'END'): return 'back'                      # results: 'back', ('open', App), None
def make(): return Hello()
```

Helpers: `shell.ListMenu`, `shell.PickList`, `shell.Popup`, `shell.TextBox`, `shell.Editor` (text entry), `shell.notify(title, text)`,
`shell.http_get(url)`, `shell.DATA` (persistent storage folder `/data/jgm`), `shell.sh(cmd)`.
Read `system/files/jgmshell.py` for the full set. The app folder is on `sys.path`, so `import helper` works for sibling files.
