// Cole este código em script.google.com, logado na conta da agenda (ex.: a do trabalho).
// Ele publica os próximos eventos para o servidor do painel, em JSON.

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
  var today = Utilities.formatDate(now, TZ, 'yyyy-MM-dd');
  var items = [];
  CalendarApp.getAllCalendars().forEach(function (cal) {
    if (!cal.isSelected()) return; // só agendas visíveis no Google Agenda
    cal.getEvents(now, end).forEach(function (ev) {
      if (ev.getMyStatus() === CalendarApp.GuestStatus.NO) return; // convites recusados
      var startDay = Utilities.formatDate(ev.getStartTime(), TZ, 'yyyy-MM-dd');
      // Começou num dia anterior (evento de vários dias): aparece em hoje, sem horário.
      var day = startDay < today ? today : startDay;
      var timed = !ev.isAllDayEvent() && startDay === day;
      var endDay = Utilities.formatDate(ev.getEndTime(), TZ, 'yyyy-MM-dd');
      items.push({
        date: day,
        time: timed ? Utilities.formatDate(ev.getStartTime(), TZ, 'HH:mm') : null,
        end: timed && endDay === day ? Utilities.formatDate(ev.getEndTime(), TZ, 'HH:mm') : null,
        title: ev.getTitle() || '(sem título)'
      });
    });
  });
  return ContentService.createTextOutput(JSON.stringify(items))
    .setMimeType(ContentService.MimeType.JSON);
}
