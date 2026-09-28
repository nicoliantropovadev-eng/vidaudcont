/**
 * VidAudCont ↔ Google Таблица.
 *
 * Установка (один раз, ~3 минуты):
 *  1. В таблице: Расширения → Apps Script. Удалите всё в редакторе и вставьте этот код целиком
 *     (программа кладёт его в буфер обмена уже со своим ключом).
 *  2. Нажмите «Сохранить» (значок дискеты).
 *  3. «Начать развёртывание» → «Новое развёртывание» → шестерёнка → «Веб-приложение».
 *     Запуск от имени: «Я». Доступ: «Все». Нажмите «Начать развёртывание» и разрешите доступ.
 *  4. Скопируйте «URL веб-приложения» и вставьте его в программу (Таблица… → Адрес).
 *
 * Ключ ниже защищает таблицу: без него запросы отклоняются. Не публикуйте его.
 */
const KEY = '__VIDAUDCONT_KEY__';
const VERSION = 3;  // 2: row colours (rows marked red are skipped); 3: transcripts one after another

function doGet(e) {
  return handle_(e && e.parameter ? e.parameter : {});
}

function doPost(e) {
  let p;
  try {
    p = JSON.parse(e.postData.contents);
  } catch (err) {
    return out_({ok: false, error: 'bad request'});
  }
  return handle_(p);
}

function handle_(p) {
  if (!p || p.key !== KEY) return out_({ok: false, error: 'wrong key'});
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = p.sheet ? ss.getSheetByName(p.sheet) : ss.getSheets()[0];
  if (!sh) return out_({ok: false, error: 'no sheet: ' + p.sheet});
  const cols = {link: Number(p.link_col || 3), cuts: Number(p.cuts_col || 4), note: Number(p.note_col || 5)};
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    if (p.action === 'ping') {
      return out_({ok: true, version: VERSION, spreadsheet: ss.getName(), sheet: sh.getName(), last_row: sh.getLastRow()});
    }
    if (p.action === 'rows') {
      const last = sh.getLastRow();
      const width = Math.max(cols.link, cols.cuts, cols.note);
      const range = last > 0 ? sh.getRange(1, 1, last, width) : null;
      const vals = range ? range.getDisplayValues() : [];
      const bgs = range ? range.getBackgrounds() : [];
      const fgs = range ? range.getFontColors() : [];
      const rows = [];
      for (let i = 0; i < vals.length; i++) {
        const link = String(vals[i][cols.link - 1] || '');
        const m = /(?:[?&]v=|youtu\.be\/|\/shorts\/|\/embed\/)([A-Za-z0-9_-]{11})/.exec(link);
        if (m) {
          rows.push({row: i + 1, link: link, id: m[1],
                     cuts: String(vals[i][cols.cuts - 1] || ''), note: String(vals[i][cols.note - 1] || ''),
                     // the colours the user marks rows with: fills of the row (white left out), text of the link
                     bg: bgs[i].filter(function (c) { return c && c !== '#ffffff'; }),
                     fg: String(fgs[i][cols.link - 1] || '')});
        }
      }
      return out_({ok: true, version: VERSION, sheet: sh.getName(), rows: rows});
    }
    if (p.action === 'write') {
      const updates = p.updates || [];
      updates.forEach(function (u) {
        const row = Number(u.row);
        if (!(row >= 1)) return;
        if (u.cuts !== undefined && u.cuts !== null) {
          const c = sh.getRange(row, cols.cuts);
          c.setNumberFormat('@');  // plain text: "3:10-3:17" must not turn into a time
          c.setValue(String(u.cuts));
        }
        if (u.note !== undefined && u.note !== null) {
          const n = sh.getRange(row, cols.note);
          n.setNumberFormat('@');
          n.setValue(String(u.note));
        }
      });
      SpreadsheetApp.flush();
      return out_({ok: true, written: updates.length});
    }
    if (p.action === 'append_texts') {
      // one row per video, one after another: a video already on the sheet gets its own row updated
      const last = sh.getLastRow();
      const rowOf = {};
      if (last > 0) {
        const a = sh.getRange(1, 1, last, 1).getDisplayValues();
        for (let i = 0; i < a.length; i++) {
          const m = LINK_RE.exec(String(a[i][0] || ''));
          if (m && !(m[1] in rowOf)) rowOf[m[1]] = i + 1;
        }
      }
      let next = last + 1;
      if (last === 0) {
        sh.getRange(1, 1, 1, 2).setValues([['Ссылка', 'Расшифровка']]);
        next = 2;
      }
      const rows = [];
      (p.updates || []).forEach(function (u) {
        const cells = [String(u.link)].concat((u.parts || []).map(String));
        let row = rowOf[u.id];
        if (!row) {
          row = next++;
          rowOf[u.id] = row;
        }
        const r = sh.getRange(row, 1, 1, cells.length);
        r.setNumberFormat('@');
        r.setValues([cells]);
        rows.push(row);
      });
      SpreadsheetApp.flush();
      return out_({ok: true, written: rows.length, rows: rows});
    }
    if (p.action === 'compact') {
      // empty rows removed, everything else kept in its order
      const last = sh.getLastRow(), width = sh.getLastColumn();
      if (last < 1 || width < 1) return out_({ok: true, rows: 0, removed: 0});
      const vals = sh.getRange(1, 1, last, width).getValues();
      const kept = vals.filter(function (r) { return r.join('').trim() !== ''; });
      if (kept.length === last) return out_({ok: true, rows: kept.length, removed: 0});
      const r = sh.getRange(1, 1, kept.length, width);
      r.setNumberFormat('@');
      r.setValues(kept);
      sh.getRange(kept.length + 1, 1, last - kept.length, width).clearContent();
      SpreadsheetApp.flush();
      return out_({ok: true, rows: kept.length, removed: last - kept.length});
    }
    return out_({ok: false, error: 'unknown action: ' + p.action});
  } finally {
    lock.releaseLock();
  }
}

const LINK_RE = /(?:[?&]v=|youtu\.be\/|\/shorts\/|\/embed\/)([A-Za-z0-9_-]{11})/;

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
