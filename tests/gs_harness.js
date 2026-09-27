// Runs the Apps Script connector against an in-memory fake of the Sheets API.
// usage: node tests/gs_harness.js <connector.gs> <requests.json>   -> prints JSON responses and the final grid
const fs = require("fs");
const vm = require("vm");

const [, , gsPath, reqPath] = process.argv;
const grid = JSON.parse(fs.readFileSync(reqPath, "utf8")).grid;  // array of rows (arrays of strings)
const formats = {};

function cell(r, c) { return (grid[r - 1] || [])[c - 1] || ""; }

const sheet = {
  getName: () => "Sheet1",
  getLastRow: () => grid.length,
  getRange: (row, col, nrows, ncols) => ({
    getDisplayValues: () => {
      const out = [];
      for (let r = 0; r < (nrows || 1); r++) {
        const line = [];
        for (let c = 0; c < (ncols || 1); c++) line.push(String(cell(row + r, col + c)));
        out.push(line);
      }
      return out;
    },
    setNumberFormat: (f) => { formats[`${row},${col}`] = f; },
    setValue: (v) => {
      while (grid.length < row) grid.push([]);
      const line = grid[row - 1];
      while (line.length < col) line.push("");
      line[col - 1] = v;
    },
  }),
};
const ctx = {
  SpreadsheetApp: {
    getActiveSpreadsheet: () => ({ getName: () => "Test table", getSheets: () => [sheet],
                                   getSheetByName: (n) => (n === "Sheet1" ? sheet : null) }),
    flush: () => {},
  },
  LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
  ContentService: {
    MimeType: { JSON: "application/json" },
    createTextOutput: (s) => ({ text: s, setMimeType() { return this; } }),
  },
  JSON, String, Number, Math,
};
vm.createContext(ctx);
const src = fs.readFileSync(gsPath, "utf8").replace("__VIDAUDCONT_KEY__", "k123");
vm.runInContext(src, ctx);
const requests = JSON.parse(fs.readFileSync(reqPath, "utf8")).requests;
const responses = requests.map((q) => {
  const res = q.method === "GET" ? ctx.doGet({ parameter: q.body }) : ctx.doPost({ postData: { contents: JSON.stringify(q.body) } });
  return JSON.parse(res.text);
});
console.log(JSON.stringify({ responses, grid, formats }));
