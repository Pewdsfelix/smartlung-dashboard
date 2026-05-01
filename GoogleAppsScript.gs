/*
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║       SmartLungBox — Google Apps Script (Web App Logger)        ║
 * ╠══════════════════════════════════════════════════════════════════╣
 * ║  วิธีติดตั้ง:                                                     ║
 * ║  1. เปิด Google Sheets ใหม่                                       ║
 * ║  2. Extensions → Apps Script                                      ║
 * ║  3. วาง code นี้ทั้งหมด แทน code เดิม                             ║
 * ║  4. กด Deploy → New deployment                                    ║
 * ║     - Type: Web App                                               ║
 * ║     - Execute as: Me                                              ║
 * ║     - Who has access: Anyone                                      ║
 * ║  5. กด Deploy → Copy Web App URL                                  ║
 * ║  6. เอา URL ไปใส่ใน SHEETS_URL ใน Wemos code                      ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

// ชื่อ Sheet ที่จะบันทึกข้อมูล
var SHEET_NAME = "SmartLungBox Log";

// ─── รับข้อมูลจาก Wemos (HTTP POST) ──────────────────────────────
function doPost(e) {
  try {
    var ss    = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SHEET_NAME);

    // สร้าง sheet ใหม่ถ้ายังไม่มี
    if (!sheet) {
      sheet = ss.insertSheet(SHEET_NAME);
      setupHeaders(sheet);
    }

    // ถ้า sheet ว่าง → ใส่ header ก่อน
    if (sheet.getLastRow() === 0) {
      setupHeaders(sheet);
    }

    // Parse JSON จาก Wemos
    var data = JSON.parse(e.postData.contents);

    // Append row
    sheet.appendRow([
      new Date(),                          // A: เวลา
      data.pm25   || 0,                    // B: PM2.5 (µg/m³)
      data.co2    || 0,                    // C: CO2 (ppm)
      data.temp   || 0,                    // D: อุณหภูมิ (°C)
      data.rh     || 0,                    // E: ความชื้น (%)
      data.cai    || 0,                    // F: CAI score
      data.level  || "UNKNOWN",            // G: ระดับอากาศ
      data.fan    || 0,                    // H: พัดลม (0/1)
      data.outdoor_pm25 >= 0
        ? data.outdoor_pm25 : "N/A"        // I: PM2.5 นอกห้อง
    ]);

    return ContentService
      .createTextOutput("OK")
      .setMimeType(ContentService.MimeType.TEXT);

  } catch (err) {
    return ContentService
      .createTextOutput("ERROR: " + err.message)
      .setMimeType(ContentService.MimeType.TEXT);
  }
}

// ─── GET: ดึงข้อมูลทั้งหมด (สำหรับ Dashboard) ───────────────────
function doGet(e) {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet || sheet.getLastRow() <= 1) {
    return ContentService
      .createTextOutput(JSON.stringify([]))
      .setMimeType(ContentService.MimeType.JSON);
  }

  var lastRow = sheet.getLastRow();
  var numRows = lastRow - 1; // ไม่รวม header
  var data    = sheet.getRange(2, 1, numRows, 9).getValues();

  var rows = data.map(function(row) {
    return {
      timestamp:    row[0] instanceof Date ? row[0].toISOString() : String(row[0]),
      pm25:         Number(row[1]) || 0,
      co2:          Number(row[2]) || 0,
      temp:         Number(row[3]) || 0,
      rh:           Number(row[4]) || 0,
      cai:          Number(row[5]) || 0,
      level:        String(row[6]) || "SAFE",
      fan:          Number(row[7]) || 0,
      outdoor_pm25: row[8] === "N/A" ? null : (Number(row[8]) || null)
    };
  });

  return ContentService
    .createTextOutput(JSON.stringify(rows))
    .setMimeType(ContentService.MimeType.JSON);
}

// ─── Setup header row ─────────────────────────────────────────────
function setupHeaders(sheet) {
  var headers = [
    "Timestamp",
    "PM2.5 (µg/m³)",
    "CO2 (ppm)",
    "Temp (°C)",
    "Humidity (%)",
    "CAI Score",
    "Air Level",
    "Fan (0/1)",
    "Outdoor PM2.5"
  ];

  sheet.appendRow(headers);

  // จัดสไตล์ header
  var headerRange = sheet.getRange(1, 1, 1, headers.length);
  headerRange.setBackground("#1e8449");
  headerRange.setFontColor("#ffffff");
  headerRange.setFontWeight("bold");
  headerRange.setFontSize(11);

  // ตั้งความกว้างคอลัมน์
  sheet.setColumnWidth(1, 180);  // Timestamp
  sheet.setColumnWidth(2, 120);
  sheet.setColumnWidth(3, 100);
  sheet.setColumnWidth(4, 100);
  sheet.setColumnWidth(5, 120);
  sheet.setColumnWidth(6, 90);
  sheet.setColumnWidth(7, 100);
  sheet.setColumnWidth(8, 80);
  sheet.setColumnWidth(9, 130);

  // Freeze header row
  sheet.setFrozenRows(1);
}

// ─── สร้าง Chart อัตโนมัติ (รันครั้งเดียวหลัง deploy) ────────────
function createCharts() {
  var ss    = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) return;

  // PM2.5 Chart
  var pm25Chart = sheet.newChart()
    .setChartType(Charts.ChartType.LINE)
    .addRange(sheet.getRange("A:A"))   // time
    .addRange(sheet.getRange("B:B"))   // PM2.5
    .setPosition(5, 11, 0, 0)
    .setOption("title", "PM2.5 ในห้องเรียน (µg/m³)")
    .setOption("series", { 0: { color: "#e74c3c" } })
    .setOption("hAxis", { title: "เวลา" })
    .setOption("vAxis", { title: "µg/m³", minValue: 0 })
    .setOption("width", 500)
    .setOption("height", 300)
    .build();
  sheet.insertChart(pm25Chart);

  // CO2 Chart
  var co2Chart = sheet.newChart()
    .setChartType(Charts.ChartType.LINE)
    .addRange(sheet.getRange("A:A"))
    .addRange(sheet.getRange("C:C"))   // CO2
    .setPosition(20, 11, 0, 0)
    .setOption("title", "CO2 ในห้องเรียน (ppm)")
    .setOption("series", { 0: { color: "#3498db" } })
    .setOption("hAxis", { title: "เวลา" })
    .setOption("vAxis", { title: "ppm", minValue: 400 })
    .setOption("width", 500)
    .setOption("height", 300)
    .build();
  sheet.insertChart(co2Chart);

  Logger.log("Charts created!");
}
