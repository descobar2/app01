const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "—").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = (d) => d ? new Date(d).toLocaleString('es-GT',{dateStyle:'medium',timeStyle:'short'}) : '—';
let token = sessionStorage.getItem('soast_token');
let me = null;
const navs = {
  administrador: [['health','Salud'],['companies','Empresas'],['users','Usuarios'],['audit','Auditoría'],['issues','Errores de ingesta']],
  supervisor: [['dashboard','Resumen'],['tickets','Tickets'],['escalations','Escalaciones'],['sessions','Sesiones'],['shifts','Turnos'],['audit','Auditoría']],
  coordinador: [['dashboard','Resumen'],['tickets','Tickets'],['escalations','Escalaciones'],['sessions','Sesiones'],['shifts','Turnos'],['audit','Auditoría']],
  tecnico: [['dashboard','Mis indicadores'],['tickets','Tickets'],['sessions','Mis sesiones'],['shifts','Mis turnos']],
  gerencia: [['dashboard','Indicadores']],
  externo: [['dashboard','Resumen'],['tickets','Mis tickets'],['escalations','Escalaciones'],['sessions','Mis sesiones']]
};
function flash(message,error=false){$('feedback').textContent=message;$('feedback').className=error?'error':'';$('feedback').hidden=false;}
async function api(path,opts={}) {
  let res=await fetch('/api'+path,{...opts,headers:{...(opts.body?{'Content-Type':'application/json'}:{}),...(token?{Authorization:'Bearer '+token}:{}),...opts.headers}});
  let data=await res.json().catch(()=>({detail:'Respuesta inesperada del servidor'}));
  if(!res.ok){if(res.status===401 && token){sessionStorage.removeItem('soast_token');token=null;me=null;showLogin();}throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));}
  return data;
}
function showLogin(){
  $('login').hidden=false;$('view').hidden=true;$('sidebar').hidden=true;$('heading').textContent='Acceso a SOAST';
}
function showApp(){
  $('login').hidden=true;$('view').hidden=false;$('sidebar').hidden=false;
  $('account').textContent=me.username+' · '+me.role;
  $('nav').innerHTML=(navs[me.role]||[]).map(([key,label])=>`<button data-page="${key}">${label}</button>`).join('');
  goto(navs[me.role][0][0]);
}
function tag(v){const bad=['vencido','incumplido','cancel_pending'];const warn=['atrasado','por_vencer','sync_pending','sin_datos'];return `<span class="tag ${bad.includes(v)?'bad':warn.includes(v)?'warn':['confirmed','cumplido','en_plazo'].includes(v)?'ok':''}">${esc(v)}</span>`}
function table(headers,rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${row.map(c=>`<td>${c}</td>`).join('')}</tr>`).join(''):`<tr><td class="empty" colspan="${headers.length}">No hay resultados disponibles.</td></tr>`}</tbody></table></div>`}
async function goto(page){
  $('feedback').hidden=true;
  $('heading').textContent=(navs[me.role]||[]).find(x=>x[0]===page)?.[1] || 'SOAST';
  document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
  $('view').innerHTML='<p class="muted">Cargando información…</p>';
  try {
    if(page==='dashboard')await dashboard();
    if(page==='tickets')await tickets();
    if(page==='sessions')await sessions();
    if(page==='escalations')await escalations();
    if(page==='health')await health();
    if(page==='companies')await companies();
    if(page==='users')await users();
    if(page==='shifts')await shifts();
    if(page==='audit')await audit();
    if(page==='issues')await issues();
  }catch(e){flash(e.message,true);$('view').innerHTML='<p class="empty">No se pudo cargar la información.</p>'}
}
async function dashboard(){
 const d=await api('/dashboard');const c=d.counts;
 $('view').innerHTML=`<div class="grid">${[['Tickets visibles',d.total],['Abiertos',c.abiertos],['En progreso',c.en_progreso],['Atrasados',c.atrasados],['Por vencer',c.por_vencer],['Cumplidos',c.cumplidos],['Incumplidos',c.incumplidos],['SLA atribuible',d.compliance_pct===null?'No disponible':d.compliance_pct+'%']].map(([label,value])=>`<article class="metric"><span>${label}</span><strong>${esc(value)}</strong></article>`).join('')}</div><div class="panel" style="margin-top:20px"><h2>Alcance de los indicadores</h2><p>Los datos dependen de los registros importados y de la reconstrucción completa desde el estado COLA. Casos sin historia inicial verificable: ${esc(c.sin_datos)}. El ticket oficial permanece en el sistema propietario.</p></div>`;
}
async function tickets(){
 const data=await api('/tickets?limit=100');
 $('view').innerHTML=`<div class="panel"><div class="toolbar"><label>Buscar por ticket<input id="ticket-q" placeholder="Número de ticket"></label><button id="ticket-search">Buscar</button><button id="export-tickets">Exportar CSV</button></div><div id="ticket-result"></div></div>`;
 const render=(items)=>{$('ticket-result').innerHTML=table(['Ticket','Empresa','Producto','Estado','Nivel','Responsable','SLA','Acción'],items.map(x=>[`<b>${esc(x.legacy_id)}</b>`,esc(x.company_id),esc(x.product),tag(x.status),esc(x.level),esc(x.owner),tag(x.sla.state),`<button class="link" data-ticket="${x.id}">Ver historial</button>`]));};
 render(data);
 $('ticket-search').onclick=async()=>{try{render(await api('/tickets?q='+encodeURIComponent($('ticket-q').value)))}catch(e){flash(e.message,true)}};
 $('export-tickets').onclick=async()=>{try{const r=await fetch('/api/reports/tickets.csv',{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw Error('No se pudo exportar');const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download='soast-tickets.csv';a.click();URL.revokeObjectURL(url)}catch(e){flash(e.message,true)}};
}
async function detail(id){
 const t=await api('/tickets/'+id);
 $('view').innerHTML=`<div class="panel"><button id="back-tickets">Volver</button><div class="section-heading"><h2>Ticket ${esc(t.legacy_id)}</h2>${tag(t.status)}</div><p>Empresa ${esc(t.company_id)} · ${esc(t.product)} · Responsable: ${esc(t.owner)} · Nivel ${esc(t.level)}</p><p>SLA: ${tag(t.sla.state)} ${t.sla.hours!==null?esc(t.sla.hours)+' h / '+t.sla.limit+' h':''}</p>${!t.complete_history?'<p class="muted">Historial de origen parcial: SLA no calculable con certeza.</p>':''}<h2>Historial de eventos</h2>${table(['Fecha','Estado','Nivel','Responsable','Actor','Actividad'],t.events.map(x=>[pretty(x.date),tag(x.status),esc(x.level),esc(x.owner),esc(x.actor),esc(x.activity)]))}</div>`;
 $('back-tickets').onclick=()=>goto('tickets');
}
async function escalations(){const d=await api('/escalations');$('view').innerHTML=`<div class="panel"><h2>Cambios de nivel observados en el sistema propietario</h2>${table(['Ticket','Empresa','Nivel origen','Destino','Fecha','Ruta'],d.map(x=>[esc(x.ticket),esc(x.company_id),esc(x.from_level),esc(x.to_level),pretty(x.at),tag(x.valid_route?'Ruta válida':'Revisar ruta')]))}</div>`}
async function sessions(){
  const canBook=['externo','supervisor','coordinador'].includes(me.role);
  const [list,ticketsList]=await Promise.all([
    api('/sessions'),
    canBook?api('/tickets?limit=100'):Promise.resolve([])
  ]);
  const ticketOptions=ticketsList.filter(t=>!['CERRADO','RESUELTO'].includes(t.status));
  const booking=canBook?`
    <section class="panel schedule-panel">
      <div class="panel-lead">
        <div><h2>Programar una sesión</h2><p>Selecciona el caso, consulta los horarios y envía la invitación de Microsoft Teams.</p></div>
      </div>
      <form id="book-form" class="organized-form booking-form">
        <div class="form-step"><span class="step-number">1</span><strong>Datos de la solicitud</strong></div>
        <label class="field-ticket">Ticket de soporte
          <select name="ticket_id" required ${ticketOptions.length?'':'disabled'}>
            <option value="">Selecciona un ticket</option>
            ${ticketOptions.map(t=>`<option value="${t.id}">#${esc(t.legacy_id)} · ${esc(t.product)} · ${esc(t.level)}</option>`).join('')}
          </select>
        </label>
        <label class="field-day">Fecha
          <input name="day" type="date" required>
        </label>
        <label class="field-duration">Duración
          <select name="duration">
            <option value="30">30 minutos</option><option value="60">60 minutos</option>
            <option value="90">90 minutos</option><option value="120">120 minutos</option>
          </select>
        </label>
        <div class="form-step"><span class="step-number">2</span><strong>Disponibilidad</strong></div>
        <label class="field-slots">Horarios disponibles
          <select name="starts_at" id="slots" required disabled><option value="">Primero consulta la disponibilidad</option></select>
        </label>
        <button type="button" id="find-slots" class="secondary-action" ${ticketOptions.length?'':'disabled'}>Consultar horarios</button>
        <div class="form-step"><span class="step-number">3</span><strong>Invitación y confirmación</strong></div>
        <label class="field-email">Correo del invitado
          <input name="invitee_email" type="email" placeholder="usuario@empresa.com" autocomplete="email" required>
        </label>
        <button class="primary action-book" ${ticketOptions.length?'':'disabled'}>Reservar sesión</button>
      </form>
      ${ticketOptions.length?'':`<div class="notice-inline">No hay tickets abiertos elegibles para programar sesiones.</div>`}
      <div class="notice-inline">Los horarios se calculan con turnos y reservas internas. La disponibilidad del calendario de Microsoft 365 aún no se comprueba automáticamente.</div>
    </section>`:'';
  const sessionRows=list.map(s=>[
    `<strong>#${esc(s.ticket)}</strong>`,
    esc(s.technician),pretty(s.starts_at),tag(s.state),
    s.teams_url?`<a href="${esc(s.teams_url)}" target="_blank" rel="noopener noreferrer">Unirse por Teams</a>`:'Pendiente de enlace',
    canBook&&!['cancelled','cancel_pending'].includes(s.state)?`<button data-cancel="${s.id}" class="small-action">Cancelar</button>`:'—'
  ]);
  $('view').innerHTML=`<div class="schedule-page">
    ${booking}
    <section class="panel schedule-panel">
      <div class="panel-lead"><div><h2>Sesiones programadas</h2><p>Consulta el técnico asignado, el estado y el acceso a la reunión.</p></div><span class="count-pill">${list.length} sesiones</span></div>
      ${list.length?table(['Ticket','Técnico','Inicio','Estado','Reunión','Acción'],sessionRows):
      `<div class="empty-state"><strong>Aún no hay sesiones programadas</strong><p>Cuando se registre una reserva, aparecerá aquí junto con su enlace de Teams y su estado.</p></div>`}
    </section>
  </div>`;
  if(!canBook)return;
  const form=$('book-form');
  const selector=$('slots');
  const resetSlots=()=>{selector.disabled=true;selector.innerHTML='<option value="">Consulta primero la disponibilidad</option>';};
  for(const name of ['ticket_id','day','duration'])form.elements[name].addEventListener('change',resetSlots);
  $('find-slots').onclick=async()=>{
    const f=new FormData(form);
    if(!f.get('ticket_id')||!f.get('day')){flash('Selecciona un ticket y una fecha para consultar horarios.',true);return;}
    resetSlots();
    try{
      const url='/availability?ticket_id='+encodeURIComponent(f.get('ticket_id'))+'&day='+encodeURIComponent(f.get('day'))+'&duration='+encodeURIComponent(f.get('duration'));
      const d=await api(url);
      selector.innerHTML=d.starts_at.length?d.starts_at.map(s=>`<option value="${esc(s)}">${pretty(s)}</option>`).join(''):'<option value="">Sin horarios disponibles para esta fecha</option>';
      selector.disabled=!d.starts_at.length;
      if(d.starts_at.length)flash(d.starts_at.length+' horarios disponibles. Selecciona uno para reservar.');
      else flash('No hay horarios disponibles. Comprueba los turnos y que existan técnicos activos.',true);
    }catch(e){flash(e.message,true);}
  };
  form.onsubmit=async e=>{
    e.preventDefault();
    const f=new FormData(form);
    if(!f.get('starts_at')){flash('Primero consulta y selecciona un horario disponible.',true);return;}
    try{
      const s=await api('/sessions',{method:'POST',body:JSON.stringify({
        ticket_id:Number(f.get('ticket_id')),starts_at:f.get('starts_at'),
        duration:Number(f.get('duration')),invitee_email:f.get('invitee_email')
      })});
      await goto('sessions');
      flash('Sesión registrada. Estado: '+s.state);
    }catch(error){flash(error.message,true);}
  };
}
async function health(){const d=await api('/health');$('view').innerHTML=`<div class="grid"><article class="metric"><span>API</span><strong>${esc(d.service)}</strong></article><article class="metric"><span>Base de datos</span><strong>${esc(d.database)}</strong></article><article class="metric"><span>Eventos importados</span><strong>${esc(d.events)}</strong></article><article class="metric"><span>Microsoft Graph</span><strong style="font-size:17px">${d.graph_configured?'Configurado':'Pendiente'}</strong></article></div><div class="panel" style="margin-top:20px"><h2>Importación local</h2><p>Ejecutar en el servidor los comandos documentados de importación. No adjuntar datos reales al repositorio público.</p></div>`}
async function companies(){let data=await api('/companies');$('view').innerHTML=`<div class="panel"><h2>Agregar empresa</h2><form id="company-form" class="box-form"><label>Código<input name="code" required></label><label>Nombre<input name="name" required></label><button class="primary">Guardar</button></form></div><div class="panel" style="margin-top:20px"><h2>Empresas registradas (${data.length})</h2>${table(['Código','Empresa','Estado'],data.map(c=>[esc(c.code),esc(c.name),tag(c.active?'Activa':'Inactiva')+` <button data-toggle-company=\"${c.id}\" data-active=\"${c.active}\">${c.active?'Desactivar':'Activar'}</button>`]))}</div>`;$('company-form').onsubmit=async(e)=>{e.preventDefault();try{await api('/companies',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await goto('companies');flash('Empresa registrada')}catch(e){flash(e.message,true)}}}
async function users(){const data=await api('/users');const companies=await api('/companies');$('view').innerHTML=`<div class="panel"><h2>Crear cuenta</h2><form id="user-form" class="box-form"><label>Usuario<input name="username" required></label><label>Contraseña inicial<input name="password" type="password" minlength="12" required></label><label>Rol<select name="role">${['externo','tecnico','supervisor','coordinador','gerencia','administrador'].map(x=>`<option>${x}</option>`).join('')}</select></label><label>Empresa (externo)<select name="company_id"><option value="">Ninguna</option>${companies.filter(x=>x.active).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select></label><label>Nivel (técnico)<select name="level"><option value="">—</option><option>L1</option><option>L2</option><option>L3</option></select></label><button class="primary">Crear</button></form></div><div class="panel" style="margin-top:20px"><h2>Cuentas</h2>${table(['Usuario','Rol','Nivel','Empresa','Activo'],data.map(x=>[esc(x.username),esc(x.role),esc(x.level),esc(x.company_id),tag(x.active?'Sí':'No')+` <button data-toggle-user=\"${x.id}\" data-active=\"${x.active}\">${x.active?'Desactivar':'Activar'}</button> <button data-reset-user=\"${x.id}\">Contraseña</button>`]))}</div>`;$('user-form').onsubmit=async(e)=>{e.preventDefault();let f=Object.fromEntries(new FormData(e.target));f.company_id=f.company_id?Number(f.company_id):null;f.level=f.level||null;try{await api('/users',{method:'POST',body:JSON.stringify(f)});await goto('users');flash('Cuenta creada')}catch(e){flash(e.message,true)}}}
async function datedShiftPanel(){
  const available=await api('/schedule-months');
  if(!available.months.length)return;
  const monthNames=available.months.map(m=>{
    const label=new Intl.DateTimeFormat('es-GT',{month:'long',year:'numeric',timeZone:'UTC'})
       .format(new Date(m+'-01T12:00:00Z'));
    return {month:m,label:label.charAt(0).toUpperCase()+label.slice(1)};
  });
  $('view').insertAdjacentHTML('beforeend',`
    <section class="panel schedule-panel" style="margin-top:22px">
      <div class="panel-lead"><div><h2>Jornadas por fecha</h2>
        <p>Historial real importado por día. No se repite automáticamente en otros meses.</p>
      </div><span class="count-pill" id="dated-count">Cargando…</span></div>
      <div class="roster-controls">
        <label>Mes
          <select id="dated-month">${monthNames.map(m=>`<option value="${esc(m.month)}">${esc(m.label)}</option>`).join('')}</select>
        </label>
        <label>Técnico
          <select id="dated-tech"><option value="">Todos los técnicos</option></select>
        </label>
        <label>Tipo
          <select id="dated-kind"><option value="">Todos los tipos</option>
            <option value="TURNO1">Turno diurno</option>
            <option value="TURNO2_00-06">Turno 00–06</option>
            <option value="TURNO2_20-24">Turno 20–24</option>
            <option value="GUARDIA_L3">Guardia L3</option>
          </select>
        </label>
        <label>Fecha
          <input id="dated-day" type="date">
        </label>
      </div>
      <div id="dated-results"></div>
      <div class="roster-footer">
        <span class="hint">Las guardias L3 se documentan, pero no se ofrecen como citas automáticas.</span>
        <button id="dated-more" type="button" hidden>Mostrar más</button>
      </div>
    </section>`);
  let rows=[], visible=50;
  const days=['Domingo','Lunes','Martes','Miércoles','Jueves','Viernes','Sábado'];
  const timeOf=n=>String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');
  const render=()=>{
    const person=$('dated-tech').value;
    const kind=$('dated-kind').value;
    const day=$('dated-day').value;
    const selected=rows.filter(s=>(!person||String(s.user_id)===person)&&(!kind||s.kind===kind)&&(!day||s.date===day));
    $('dated-count').textContent=selected.length+' jornadas';
    $('dated-results').innerHTML=selected.length?table(
      ['Fecha','Técnico','Nivel / tipo','Horario','Estado'],
      selected.slice(0,visible).map(s=>{
        const weekday=days[new Date(s.date+'T12:00:00Z').getUTCDay()];
        const type=s.kind==='GUARDIA_L3'?'Guardia L3':s.kind==='TURNO1'?'Turno diurno':s.kind==='TURNO2_00-06'?'Turno 00–06':'Turno 20–24';
        return [
          `<strong>${esc(s.date)}</strong><br><span class="muted">${weekday}</span>`,
          esc(s.username),esc(type),
          timeOf(s.start_minute)+' – '+timeOf(s.end_minute),
          tag(s.active?'Activo':'Inactivo')
        ];
      })
    ):`<div class="empty-state"><strong>Sin jornadas para estos filtros</strong><p>Selecciona otro día o técnico, o consulta otro mes.</p></div>`;
    $('dated-more').hidden=selected.length<=visible;
    $('dated-more').textContent='Mostrar más ('+Math.min(50,selected.length-visible)+' de '+(selected.length-visible)+')';
  };
  const reload=async()=>{
    visible=50;rows=await api('/dated-shifts?month='+encodeURIComponent($('dated-month').value));
    const unique=[...new Map(rows.map(r=>[r.user_id,r.username])).entries()].sort((a,b)=>a[1].localeCompare(b[1],'es'));
    $('dated-tech').innerHTML='<option value="">Todos los técnicos</option>'+unique.map(([id,name])=>`<option value="${id}">${esc(name)}</option>`).join('');
    $('dated-day').value='';
    $('dated-day').min=$('dated-month').value+'-01';
    $('dated-day').max=$('dated-month').value+'-31';
    render();
  };
  $('dated-month').onchange=()=>reload().catch(e=>flash(e.message,true));
  for(const id of ['dated-tech','dated-kind','dated-day'])
    $(id).onchange=()=>{visible=50;render();};
  $('dated-more').onclick=()=>{visible+=50;render();};
  await reload();
}

async function shifts(){
  const data=await api('/shifts');
  const canEdit=me.role==='supervisor';
  const techs=['supervisor','coordinador'].includes(me.role)?(await api('/users')).filter(u=>u.role==='tecnico'):[];
  const byId=new Map(techs.map(t=>[t.id,t]));
  const nameOf=id=>byId.get(id)?.username||(me.role==='tecnico'?'Mi turno':'Técnico #'+id);
  const days=['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];
  const timeOf=n=>String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');
  const ordered=[...data].sort((a,b)=>nameOf(a.user_id).localeCompare(nameOf(b.user_id),'es')||a.weekday-b.weekday||a.start_minute-b.start_minute);
  const form=canEdit?`
    <section class="panel schedule-panel">
      <div class="panel-lead"><div><h2>Configurar turno recurrente</h2><p>Define la jornada semanal del técnico. Los turnos se utilizan al consultar la disponibilidad de sesiones.</p></div></div>
      <form id="shift-form" class="organized-form shift-form">
        <label class="field-technician">Técnico
          <select name="user_id" required ${techs.length?'':'disabled'}>
            <option value="">Selecciona un técnico</option>
            ${techs.map(t=>`<option value="${t.id}">${esc(t.username)} · ${esc(t.level)}${t.active?'':' (inactivo)'}</option>`).join('')}
          </select>
        </label>
        <label class="field-weekday">Día de la semana
          <select name="weekday">${days.map((d,i)=>`<option value="${i}">${d}</option>`).join('')}</select>
        </label>
        <label class="field-start">Hora de inicio
          <input type="time" name="start" value="08:00" required>
        </label>
        <label class="field-end">Hora de finalización
          <input type="time" name="end" value="17:00" required>
        </label>
        <div class="form-actions"><button class="primary" ${techs.length?'':'disabled'}>Guardar turno</button></div>
      </form>
      <div class="notice-inline">Los técnicos importados inicialmente están inactivos. Para asignarles sesiones deben estar activos y tener un turno registrado.</div>
    </section>`:'';
  $('view').innerHTML=`<div class="schedule-page">
    ${form}
    <section class="panel schedule-panel">
      <div class="panel-lead"><div><h2>Turnos registrados</h2><p>Jornadas semanales por técnico, expresadas en horario de Guatemala.</p></div><span class="count-pill">${data.length} turnos</span></div>
      ${techs.length&&data.length?`<div class="shift-toolbar"><label for="shift-filter">Filtrar por técnico</label><select id="shift-filter"><option value="">Todos los técnicos</option>${techs.filter(t=>data.some(s=>s.user_id===t.id)).map(t=>`<option value="${t.id}">${esc(t.username)}</option>`).join('')}</select></div>`:''}
      <div id="shift-results"></div>
    </section>
  </div>`;
  const render=(filter='')=>{
    const shown=ordered.filter(s=>!filter||String(s.user_id)===filter);
    $('shift-results').innerHTML=shown.length?table(['Técnico','Día','Horario','Duración','Cuenta'],shown.map(s=>{
      const tech=byId.get(s.user_id);
      const duration=(s.end_minute-s.start_minute)/60;
      return [
        `<strong>${esc(nameOf(s.user_id))}</strong>`,
        esc(days[s.weekday]||'—'),
        `${timeOf(s.start_minute)} – ${timeOf(s.end_minute)}`,
        `${Number.isInteger(duration)?duration:duration.toFixed(2)} h`,
        tech?tag(tech.active?'Activo':'Inactivo'):'—'
      ];
    })):`<div class="empty-state"><strong>${filter?'Este técnico no tiene turnos':'Aún no hay turnos registrados'}</strong><p>${canEdit?'Utiliza el formulario superior para registrar la primera jornada.':'El supervisor debe configurar las jornadas de los técnicos.'}</p></div>`;
  };
  render();
  if($('shift-filter'))$('shift-filter').onchange=e=>render(e.target.value);
  await datedShiftPanel();
  if(!canEdit)return;
  $('shift-form').onsubmit=async e=>{
    e.preventDefault();
    const f=new FormData(e.target);
    const min=t=>Number(t.split(':')[0])*60+Number(t.split(':')[1]);
    const start=min(f.get('start')),end=min(f.get('end'));
    if(end<=start){flash('La hora de finalización debe ser posterior a la hora de inicio.',true);return;}
    try{
      await api('/shifts',{method:'POST',body:JSON.stringify({
        user_id:Number(f.get('user_id')),weekday:Number(f.get('weekday')),
        start_minute:start,end_minute:end
      })});
      await goto('shifts');flash('Turno semanal registrado correctamente.');
    }catch(error){flash(error.message,true);}
  };
}
async function audit(){const data=await api('/audit');$('view').innerHTML=`<div class="panel"><h2>Últimas operaciones</h2>${table(['Fecha','Actor','Acción','Entidad','ID','Detalle'],data.map(x=>[pretty(x.date),esc(x.actor_id),esc(x.action),esc(x.entity),esc(x.entity_id),esc(x.detail)]))}</div>`}
async function issues(){const data=await api('/import-issues');$('view').innerHTML=`<div class="panel"><h2>Registros no procesados</h2>${table(['Archivo','Secuencia','Motivo'],data.map(x=>[esc(x.source),esc(x.seq),esc(x.reason)]))}</div>`}
$('login-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{const result=await api('/login',{method:'POST',body:JSON.stringify(Object.fromEntries(f))});token=result.token;sessionStorage.setItem('soast_token',token);me=await api('/me');showApp()}catch(err){flash(err.message,true)}};
$('logout').onclick=async()=>{try{await api('/logout',{method:'POST'})}catch{}token=null;me=null;sessionStorage.removeItem('soast_token');showLogin()};
$('nav').onclick=e=>{const b=e.target.closest('[data-page]');if(b)goto(b.dataset.page)};
$('view').onclick=async e=>{const t=e.target.closest('[data-ticket]');if(t)try{await detail(t.dataset.ticket)}catch(err){flash(err.message,true)};const activeUser=e.target.closest('[data-toggle-user]');if(activeUser)try{await api('/users/'+activeUser.dataset.toggleUser+'/active?active='+(activeUser.dataset.active!=='true'),{method:'PATCH'});await goto('users');flash('Estado de cuenta actualizado')}catch(err){flash(err.message,true)};const reset=e.target.closest('[data-reset-user]');if(reset){let pw=prompt('Nueva contraseña (mínimo 12 caracteres)');if(pw)try{await api('/users/'+reset.dataset.resetUser+'/password',{method:'POST',body:JSON.stringify({password:pw})});flash('Contraseña actualizada; sesiones revocadas')}catch(err){flash(err.message,true)}}const company=e.target.closest('[data-toggle-company]');if(company)try{await api('/companies/'+company.dataset.toggleCompany+'/active?active='+(company.dataset.active!=='true'),{method:'PATCH'});await goto('companies');flash('Estado de empresa actualizado')}catch(err){flash(err.message,true)};const c=e.target.closest('[data-cancel]');if(c&&confirm('¿Cancelar esta sesión?'))try{await api('/sessions/'+c.dataset.cancel+'/cancel',{method:'POST'});await goto('sessions');flash('Cancelación registrada; confirme estado externo si corresponde')}catch(err){flash(err.message,true)}};
$('clock').textContent=new Date().toLocaleDateString('es-GT',{dateStyle:'full'});
(async()=>{if(token)try{me=await api('/me');showApp();return}catch{}showLogin()})();