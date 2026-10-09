// Скрипт для вашей Google-таблицы: Расширения -> Apps Script. Вставьте этот код целиком (замените всё, что там было),
// замените ПАРОЛЬ ниже на свою длинную случайную строку (то же самое значение укажите на Render в SHEETS_TOKEN),
// затем: Развернуть -> Новое развертывание -> Тип: Веб-приложение -> Выполнять от имени: Я -> Доступ: Все -> Развернуть.

const TOKEN = 'ПАРОЛЬ';

// Пароль не короче 12 знаков; пока там стоит слово ПАРОЛЬ, скрипт никого не пускает.
function authorized_(token) {
  return TOKEN !== 'ПАРОЛЬ' && TOKEN.length >= 12 && token === TOKEN;
}

const SHEETS = {
  companies: { name: 'Компании', header: ['Название', 'ФИО директора', 'Почта', 'Основной ОКВЭД', 'ИНН', 'Дата регистрации'] },
  checked: { name: 'Проверенные', header: ['ИНН', 'Статус', 'Дата регистрации', 'Проверено'] },
};

function sheet_(key) {
  const spec = SHEETS[key];
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(spec.name);
  if (!sheet) sheet = book.insertSheet(spec.name);
  if (sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, spec.header.length).setValues([spec.header]).setFontWeight('bold');
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function append_(sheet, rows) {
  if (!rows.length) return;
  const range = sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length);
  range.setNumberFormat('@');          // всё как текст: ИНН с нулями в начале не превратится в число
  range.setValues(rows);
}

function out_(object) {
  return ContentService.createTextOutput(JSON.stringify(object)).setMimeType(ContentService.MimeType.JSON);
}

// Сервер присылает порцию строк. ИНН, которые уже есть на листе «Проверенные», пропускаются.
function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const body = JSON.parse(e.postData.contents);
    if (!authorized_(body.token)) return out_({ error: 'неверный пароль' });
    const checked = sheet_('checked');
    const known = new Set();
    if (checked.getLastRow() > 1) {
      checked.getRange(2, 1, checked.getLastRow() - 1, 1).getValues().forEach(function (row) { known.add(String(row[0])); });
    }
    const fresh = (body.rows || []).filter(function (row) {
      const inn = String(row.inn);
      if (known.has(inn)) return false;
      known.add(inn);
      return true;
    });
    const now = new Date().toISOString();
    append_(checked, fresh.map(function (r) { return [String(r.inn), r.status, r.date, now]; }));
    append_(sheet_('companies'), fresh.filter(function (r) { return r.status === 'ok'; }).map(function (r) {
      return [r.name, r.director, r.email, r.okved, String(r.inn), r.date];
    }));
    return out_({ ok: true, added: fresh.length });
  } catch (err) {
    return out_({ error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

// Чтение таблицы из Python: ?token=ПАРОЛЬ&sheet=companies (или checked).
function doGet(e) {
  const params = e.parameter || {};
  if (!authorized_(params.token)) return out_({ error: 'неверный пароль' });
  const key = params.sheet === 'checked' ? 'checked' : 'companies';
  const values = sheet_(key).getDataRange().getValues();
  return out_({
    header: values[0],
    rows: values.slice(1).map(function (row) { return row.map(String); }),
  });
}
