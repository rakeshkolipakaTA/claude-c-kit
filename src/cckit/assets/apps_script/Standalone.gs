/**
 * Only for a standalone Apps Script project (not the Access Hub): `cckit api export --standalone` adds this file.
 * Bind the script to the spreadsheet that should receive the data (or set Script Property USAGE_SHEET_ID).
 */
function doGet(e) {
  if (e && e.parameter && e.parameter.api === 'ping') return apiPing_();
  return ContentService.createTextOutput('Claude usage API').setMimeType(ContentService.MimeType.TEXT);
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Claude usage').addItem('Set up Claude usage API', 'setupUsageApi').addToUi();
}
