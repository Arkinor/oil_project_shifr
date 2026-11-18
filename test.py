"""test.py — прогон фраз через тот же конвейер LM Studio, что и основной сервис.

В CASES захардкожены фразы и слова. Весь список отправляется в модель как один
документ (строки через перевод строки) — так же, как сервис обрабатывает
извлечённый текст файла. Ответ модели применяется к каждой фразе теми же
функциями, что и при обходе PDF/Word/Excel. Результат пишется в test.json
рядом со скриптом/сборкой:

    Отправляем: [["Карпатый А.Ф"]]
    Получаем:   [["Карпатый А.Ф", "Злобин Г.Ж"]]

Если модель ничего не нашла, вторым элементом остаётся исходная фраза:
    [["Поставщик", "Поставщик"]]. При ошибке модели — строка, начинающаяся
    с «ОШИБКА:».

Запуск:  .venv\\Scripts\\python.exe test.py
Сборка:  testbuld.bat  ->  dist/PIIMaskerTest.exe
"""
import json
import sys
from typing import List, Tuple

import processing
from config import APP_DIR, load_settings


# ================================================================
# 1. ДАННЫЕ ДЛЯ ПРОВЕРКИ
# ================================================================
# Каждый внутренний массив — одна фраза. После прогона к ней добавляется
# результат модели. Список можно свободно дополнять и менять.
CASES: List[List[str]] = [
    # ФИО, фамилии, инициалы
    ["Карпатый А.Ф"],
    ["Иванов Иван Иванович"],
    ["ПЕТРОВ ПЁТР ПЕТРОВИЧ"],
    ["Сидорова Анна-Мария Сергеевна"],
    ["Кузнецов И. И."],
    ["Смирнов"],
    ["Мария Ивановна"],

    # Организации
    ["ООО «Ромашка»"],
    ['АО "ТехноПром"'],
    ["ПАО Сбербанк"],
    ["ИП Смирнов Алексей Владимирович"],
    ["Банк получателя: АО «Альфа-Банк»"],

    # Адреса
    ["г. Москва, ул. Ленина, д. 15, кв. 42"],
    ["192102, г. Санкт-Петербург, Московский пр-т, д. 10, офис 5"],
    ["Московская область, г. Химки, ул. Победы, д. 3"],
    ["Адрес для корреспонденции: 192102, Санкт-Петербург, ул. Фучика, д. 4, лит. А"],
    ["Москва"],

    # Реквизиты
    ["ИНН 7701234567"],
    ["7701234567"],
    ["КПП 770101001"],
    ["ОГРН 1027700132195"],
    ["ОГРНИП 304500116000157"],
    ["СНИЛС 123-456-789 00"],
    ["БИК 044525225"],
    ["р/с 40702810400000012345"],
    ["к/с 30101810400000000225"],
    ["паспорт серия 45 06 номер 123456"],

    # Контакты
    ["+7 (495) 123-45-67"],
    ["8 916 555-44-33"],
    ["ivan.petrov@example.ru"],
    ["https://example.ru/contacts"],
    ["Telegram: @ivan_petrov"],

    # Суммы, даты и сроки
    ["1 234 567,89 руб."],
    ["НДС 20% — 205 761,32 руб."],
    ["пятьсот тысяч рублей 00 копеек"],
    ["12.03.2024"],
    ["01 января 2025 г."],
    ["срок поставки — 30 календарных дней"],

    # Должности и подписи
    ["Генеральный директор"],
    ["Подпись _______ /Иванов И.И./"],
    ["Главный бухгалтер Сидорова А.С."],

    # Товары и услуги
    ["Цемент М500, 50 кг"],
    ["Ноутбук Lenovo ThinkPad T14"],
    ["Услуги по разработке сайта"],

    # Смешанные фразы
    ["Иванов Иван Иванович, ИНН 7701234567, тел. +7 (495) 123-45-67"],
    ["Оплатить ООО «Ромашка» 150 000 руб. до 01.04.2025"],
    ["Директору ООО «Ромашка» Карпатому А.Ф. от Петрова И.И."],

    # Контрольные примеры: подписи полей и заголовки менять нельзя
    ["Договор поставки № 15"],
    ["Поставщик"],
    ["Покупатель"],
    ["Итого"],
    ["Количество"],
    ["М.П."],
]


# ================================================================
# 2. ФАЙЛ РЕЗУЛЬТАТОВ
# ================================================================
TEST_FILE = APP_DIR / "test.json"


def _load_rows() -> List[List[str]]:
    """Читает прошлые ответы, чтобы обновлять их, а не терять при перезапуске."""
    if not TEST_FILE.exists():
        return []
    try:
        data = json.loads(TEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [[str(item) for item in row] for row in data
            if isinstance(row, list) and row]


def _save_rows(rows: List[List[str]]) -> None:
    tmp = TEST_FILE.with_name(f".{TEST_FILE.name}.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(TEST_FILE)


# ================================================================
# 3. ПРОГОН ЧЕРЕЗ МОДЕЛЬ
# ================================================================
def _collect_phrases() -> List[str]:
    phrases: List[str] = []
    seen = set()
    for case in CASES:
        phrase = " ".join(str(part) for part in case).strip()
        if phrase and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)
    return phrases


def _run_document(text: str, phrases: List[str]) -> Tuple[List[List[str]], str]:
    """Один запрос на весь текст — как обработка одного документа сервисом."""
    try:
        replacements, ok, error = processing.anonymize_text(text)
    except Exception as e:
        # Сбой клиента/настроек не должен обрывать прогон без результата:
        # показываем причину так же, как ошибку модели.
        return [], f"{type(e).__name__}: {e}"
    if not ok:
        return [], processing._llm_failure_message(error)
    # Те же помощники, что используют process_pdf/process_docx/process_excel.
    extras = processing._replacement_extras(replacements)
    masker = processing._build_replacement_masker(replacements)
    rows = [[phrase, str(processing.mask_text(phrase, extras, masker))]
            for phrase in phrases]
    return rows, ""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = load_settings()
    print(f"LM Studio: {settings.get('lm_studio_url')} | "
          f"модель: {settings.get('model')} | фраз: {len(CASES)}")
    if not settings.get("llm_enabled", True):
        print("LLM отключена в settings.json")
        return 1

    print("Загрузка модели...")
    if not processing.warm_up_llm(settings, report=lambda _, stage: print(
            f"  {stage}", flush=True)):
        print("Модель в LM Studio не загрузилась — проверьте настройки и сервер")
        return 1

    phrases = _collect_phrases()
    text = "\n".join(phrases)
    print(f"Отправка {len(phrases)} фраз одним документом ({len(text)} символов)...")
    rows, error = _run_document(text, phrases)
    if error:
        print(f"Ошибка модели: {error}")
        rows = [[phrase, "ОШИБКА: " + error] for phrase in phrases]

    saved = _load_rows()
    index = {str(row[0]): row for row in saved}
    for row in rows:
        existing = index.get(str(row[0]))
        if existing is not None:
            existing[:] = row
        else:
            saved.append(row)
            index[str(row[0])] = row
    _save_rows(saved)

    for phrase, result in rows:
        print(f"{phrase} -> {result}")
    failed = sum(1 for _, result in rows if str(result).startswith("ОШИБКА:"))
    print(f"\nГотово: {TEST_FILE} | успешно: {len(rows) - failed}, ошибок: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем")
        raise SystemExit(130)
