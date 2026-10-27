"""Convert the approved brand artwork to a multi-resolution Windows icon."""
from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[1]
icon = Image.open(root / 'assets' / 'stock-and-sleeve.png').convert('RGBA')
icon.save(root / 'assets' / 'stock-and-sleeve.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
