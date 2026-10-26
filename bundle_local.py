"""Build one offline HTML file without a package manager or remote assets."""
from pathlib import Path

root = Path(__file__).resolve().parent
dist = root / 'dist'
html = (dist / 'index.html').read_text(encoding='utf-8')
css = (dist / 'styles.css').read_text(encoding='utf-8')
js = (dist / 'player.js').read_text(encoding='utf-8')
assert '</script' not in js.lower()
assert '<link rel="stylesheet" href="./styles.css">' in html
assert '<script defer src="./player.js"></script>' in html
html = html.replace('<link rel="stylesheet" href="./styles.css">', '<style>\n' + css + '\n</style>')
html = html.replace('<script defer src="./player.js"></script>',
                    '<script>\ndocument.addEventListener("DOMContentLoaded", () => {\n' + js + '\n});\n</script>')
output = root / 'build' / 'offline-player.html'
output.parent.mkdir(exist_ok=True)
output.write_text(html, encoding='utf-8')
print(output)
