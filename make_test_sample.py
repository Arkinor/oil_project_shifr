"""Создаёт тестовые PDF и Excel с ПДн для ручной проверки обезличивания.

Запуск:  .venv\\Scripts\\python.exe make_test_sample.py
Файлы:   data/test_samples/sample.pdf и data/test_samples/sample.xlsx
"""
from pathlib import Path

import openpyxl
import pymupdf

OUT = Path(__file__).parent / "data" / "test_samples"
OUT.mkdir(parents=True, exist_ok=True)

arial = Path(r"C:\Windows\Fonts\arial.ttf")
arial_bold = Path(r"C:\Windows\Fonts\arialbd.ttf")
if not arial.exists():
    arial = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    arial_bold = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

font_regular = pymupdf.Font(fontfile=str(arial))
font_bold = pymupdf.Font(fontfile=str(arial_bold))

doc = pymupdf.open()
page = doc.new_page()
page.insert_font(fontname="FR", fontfile=str(arial))
page.insert_font(fontname="FB", fontfile=str(arial_bold))

y = 60


def line(parts):
    """parts: [(текст, жирный), ...] — разные span на одной строке."""
    global y
    x = 50
    for text, bold in parts:
        page.insert_text((x, y), text, fontname="FB" if bold else "FR", fontsize=11)
        x += (font_bold if bold else font_regular).text_length(text, fontsize=11)
    y += 22


line([("ДОГОВОР № 15 от 01.02.2024", False)])
line([("Заказчик: ", False), ("ООО «Ромашка»", True)])
line([("Адрес: г. Москва, ул. Тверская, д. 1, кв. 5", False)])
line([("ФИО: ", False), ("Иванов", True), (" Иван Иванович", False)])
line([("Телефон: +7 (999) 123-45-67, email: ivanov@example.ru", False)])
line([("Электронная подпись: ", False), ("Иванов И.И.", True), (" / ООО «Ромашка»", False)])
line([("ИНН 7707083893, ОГРН 1027700132195, КПП 770701001", False)])
doc.save(OUT / "sample.pdf")
doc.close()

wb = openpyxl.Workbook()
ws = wb.active
ws["A1"], ws["B1"], ws["C1"] = "Иванов", "Иван", "Иванович"
ws["A2"] = "ООО «Ромашка»"
ws["A3"] = "г. Москва, ул. Тверская, д. 1, кв. 5"
ws["A4"] = "ivanov@example.ru"
ws["A5"] = 7707083893
wb.save(OUT / "sample.xlsx")

print(f"Готово: {OUT / 'sample.pdf'} и {OUT / 'sample.xlsx'}")
