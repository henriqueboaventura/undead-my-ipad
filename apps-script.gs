// Cole este código em script.google.com (logado na conta @gmail.com).
// Ele publica os próximos eventos da sua agenda para o servidor do painel.

// Troque por uma senha longa e aleatória; ela vai no fim da URL no config.json.
var TOKEN = 'TROQUE-POR-UMA-SENHA-LONGA';
var DAYS = 14;
var TZ = 'America/Sao_Paulo';

function doGet(e) {
  if (!e || !e.parameter || e.parameter.token !== TOKEN) {
    return ContentService.createTextOutput('forbidden');
  }
  var now = new Date();
  var end = new Date(now.getTime() + DAYS * 86400000);
  var items = [];
  CalendarApp.getAllCalendars().forEach(function (cal) {
    if (!cal.isSelected()) return; // só agendas visíveis no Google Agenda
    cal.getEvents(now, end).forEach(function (ev) {
      if (ev.getMyStatus() === CalendarApp.GuestStatus.NO) return; // convites recusados
      var start = ev.getStartTime();
      var started = start < now;
      items.push({
        date: Utilities.formatDate(started ? now : start, TZ, 'yyyy-MM-dd'),
        time: ev.isAllDayEvent() || started ? null : Utilities.formatDate(start, TZ, 'HH:mm'),
        title: ev.getTitle() || '(sem título)'
      });
    });
  });
  return ContentService.createTextOutput(JSON.stringify(items))
    .setMimeType(ContentService.MimeType.JSON);
}
