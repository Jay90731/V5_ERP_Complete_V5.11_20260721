import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const historyPath = process.argv[2];
const outputPath = process.argv[3];
const previewPath = process.argv[4] || "";

if (!historyPath || !outputPath) {
  throw new Error("Usage: node build_current_orders_excel.mjs order_history.json current_orders.xlsx [preview.png]");
}

const history = JSON.parse(await fs.readFile(historyPath, "utf8"));
const targetDepartment = String(history.target_department || "4001");
const headers = [
  "訂單分類",
  "訂單編號",
  "訂貨日期",
  "禮盒代號",
  "定價",
  "數量",
  "總金額",
  "折後金額",
];

const orders = Object.values(history.orders || {})
  .map((entry) => entry.current_version)
  .filter(Boolean)
  .filter((order) =>
    String(order.department_code || "") === targetDepartment &&
    order.excel_eligible === true &&
    order.voided !== true
  )
  .sort((left, right) =>
    String(left.order_date || "").localeCompare(String(right.order_date || "")) ||
    String(left.order_no || "").localeCompare(String(right.order_no || ""))
  );

const rows = [];
for (const order of orders) {
  for (const detail of order.details || []) {
    rows.push([
      order.category || "",
      order.order_no || "",
      order.order_date ? new Date(`${order.order_date}T00:00:00`) : null,
      detail.gift_code || "",
      detail.list_price ?? null,
      detail.quantity ?? null,
      detail.total_amount ?? null,
      detail.discount_amount ?? null,
    ]);
  }
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("目前4001結果");
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(1);
sheet.getRange("A1:H1").values = [headers];
sheet.getRange("A1:H1").format = {
  fill: "#1F4E79",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
sheet.getRange("A1:H1").format.rowHeight = 26;

if (rows.length) {
  sheet.getRangeByIndexes(1, 0, rows.length, headers.length).values = rows;
  const body = sheet.getRangeByIndexes(1, 0, rows.length, headers.length);
  body.format.borders = {
    insideHorizontal: { style: "thin", color: "#D9E2EC" },
    insideVertical: { style: "thin", color: "#E7EDF3" },
  };
  sheet.getRangeByIndexes(1, 2, rows.length, 1).format.numberFormat = "yyyy-mm-dd";
  sheet.getRangeByIndexes(1, 4, rows.length, 4).format.numberFormat = "#,##0";
  sheet.getRangeByIndexes(1, 4, rows.length, 4).format.horizontalAlignment = "right";
}

const widths = [14, 18, 14, 14, 12, 10, 14, 14];
widths.forEach((width, index) => {
  sheet.getRangeByIndexes(0, index, 1, 1).format.columnWidth = width;
});

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

const inspection = await workbook.inspect({
  kind: "sheet,table",
  maxChars: 3000,
  tableMaxRows: 5,
  tableMaxCols: 8,
});
console.log(inspection.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

if (previewPath) {
  await fs.mkdir(path.dirname(previewPath), { recursive: true });
  const preview = await workbook.render({
    sheetName: "目前4001結果",
    range: `A1:H${Math.min(rows.length + 1, 30)}`,
    scale: 1.5,
    format: "png",
  });
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
}

console.log(JSON.stringify({
  output: outputPath,
  targetDepartment,
  orderCount: orders.length,
  detailRowCount: rows.length,
}, null, 2));
