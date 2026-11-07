const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "—").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = (d) => d ? new Date(d).toLocaleString('es-GT',{dateStyle:'medium',timeStyle:'short'}) : '—';
let token = sessionStorage.getItem('soast_token');
let me = null;
const navs = {
  administrador: [['health','Salud'],['companies','Empresas'],['users','Usuarios'],['audit','Auditoría'],['issues','Errores de ingesta']],
  supervisor: [['dashboard','Resumen'],['tickets','Tickets'],['sessions','Sesiones'],['shifts','Turnos'],['audit','Auditoría']],
  coordinador: [['dashboard','Resumen'],['tickets','Tickets'],['sessions','Sesiones'],['shifts','Turnos'],['audit','Auditoría']],
  tecnico: [['dashboard','Mis indicadores'],['tickets','Tickets'],['sessions','Mis sesiones'],['shifts','Mis turnos']],
  gerencia: [['dashboard','Indicadores']],
  externo: [['dashboard','Resumen'],['tickets','Mis tickets'],['sessions','Mis sesiones']]
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
 $('view').innerHTML=`<div class="panel"><div class="toolbar"><label>Buscar por ticket<input id="ticket-q" placeholder="Número de ticket"></label><button id="ticket-search">Buscar</button></div><div id="ticket-result"></div></div>`;
 const render=(items)=>{$('ticket-result').innerHTML=table(['Ticket','Empresa','Producto','Estado','Nivel','Responsable','SLA','Acción'],items.map(x=>[`<b>${esc(x.legacy_id)}</b>`,esc(x.company_id),esc(x.product),tag(x.status),esc(x.level),esc(x.owner),tag(x.sla.state),`<button class="link" data-ticket="${x.id}">Ver historial</button>`]));};
 render(data);
 $('ticket-search').onclick=async()=>{try{render(await api('/tickets?q='+encodeURIComponent($('ticket-q').value)))}catch(e){flash(e.message,true)}};
}
async function detail(id){
 const t=await api('/tickets/'+id);
 $('view').innerHTML=`<div class="panel"><button id="back-tickets">Volver</button><div class="section-heading"><h2>Ticket ${esc(t.legacy_id)}</h2>${tag(t.status)}</div><p>Empresa ${esc(t.company_id)} · ${esc(t.product)} · Responsable: ${esc(t.owner)} · Nivel ${esc(t.level)}</p><p>SLA: ${tag(t.sla.state)} ${t.sla.hours!==null?esc(t.sla.hours)+' h / '+t.sla.limit+' h':''}</p>${!t.complete_history?'<p class="muted">Historial de origen parcial: SLA no calculable con certeza.</p>':''}<h2>Historial de eventos</h2>${table(['Fecha','Estado','Nivel','Responsable','Actor','Actividad'],t.events.map(x=>[pretty(x.date),tag(x.status),esc(x.level),esc(x.owner),esc(x.actor),esc(x.activity)]))}</div>`;
 $('back-tickets').onclick=()=>goto('tickets');
}
async function sessions(){
 const list=await api('/sessions');
 const canBook=['externo','supervisor','coordinador'].includes(me.role);
 $('view').innerHTML=`${canBook?`<div class="panel"><h2>Programar sesión</h2><form id="book-form" class="box-form"><label>ID de ticket en SOAST<input name="ticket_id" type="number" min="1" required></label><label>Fecha<input name="day" type="date" required></label><label>Duración<select name="duration"><option>30</option><option>60</option><option>90</option><option>120</option></select></label><button type="button" id="find-slots">Consultar horarios</button><label>Horarios disponibles<select name="starts_at" id="slots" required><option value="">Consulta primero</option></select></label><label>Correo del invitado<input name="invitee_email" type="email" required></label><button class="primary">Reservar</button></form><p class="hint">Las franjas corresponden a turnos internos; disponibilidad de Microsoft 365 pendiente de validación.</p></div>`:''}<div class="panel" style="margin-top:20px"><h2>Sesiones</h2>${table(['Ticket','Técnico','Inicio','Estado','Reunión','Acción'],list.map(s=>[esc(s.ticket),esc(s.technician),pretty(s.starts_at),tag(s.state),s.teams_url?`<a href="${esc(s.teams_url)}" target="_blank" rel="noopener noreferrer">Abrir Teams</a>`:'Pendiente',canBook&&s.state!=='cancelled'?`<button data-cancel="${s.id}">Cancelar</button>`:'—']))}</div>`;
 if(canBook){
   $('find-slots').onclick=async()=>{let f=new FormData($('book-form'));try{let d=await api('/availability?ticket_id='+encodeURIComponent(f.get('ticket_id'))+'&day='+encodeURIComponent(f.get('day'))+'&duration='+encodeURIComponent(f.get('duration')));$('slots').innerHTML=d.starts_at.length?d.starts_at.map(s=>`<option value="${esc(s)}">${pretty(s)}</option>`).join(''):'<option value="">Sin horarios disponibles</option>';if(d.calendar_status==='local_only')flash(d.notice);}catch(e){flash(e.message,true)}};
   $('book-form').onsubmit=async(e)=>{e.preventDefault();let f=new FormData(e.target);try{let s=await api('/sessions',{method:'POST',body:JSON.stringify({ticket_id:Number(f.get('ticket_id')),starts_at:f.get('starts_at'),duration:Number(f.get('duration')),invitee_email:f.get('invitee_email')})});await goto('sessions');flash('Sesión registrada: '+s.state);}catch(e){flash(e.message,true)}};
 }
}
async function health(){const d=await api('/health');$('view').innerHTML=`<div class="grid"><article class="metric"><span>API</span><strong>${esc(d.service)}</strong></article><article class="metric"><span>Base de datos</span><strong>${esc(d.database)}</strong></article><article class="metric"><span>Eventos importados</span><strong>${esc(d.events)}</strong></article><article class="metric"><span>Microsoft Graph</span><strong style="font-size:17px">${d.graph_configured?'Configurado':'Pendiente'}</strong></article></div><div class="panel" style="margin-top:20px"><h2>Importación local</h2><p>Ejecutar en el servidor los comandos documentados de importación. No adjuntar datos reales al repositorio público.</p></div>`}
async function companies(){let data=await api('/companies');$('view').innerHTML=`<div class="panel"><h2>Agregar empresa</h2><form id="company-form" class="box-form"><label>Código<input name="code" required></label><label>Nombre<input name="name" required></label><button class="primary">Guardar</button></form></div><div class="panel" style="margin-top:20px"><h2>Empresas registradas (${data.length})</h2>${table(['Código','Empresa','Estado'],data.map(c=>[esc(c.code),esc(c.name),tag(c.active?'Activa':'Inactiva')]))}</div>`;$('company-form').onsubmit=async(e)=>{e.preventDefault();try{await api('/companies',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await goto('companies');flash('Empresa registrada')}catch(e){flash(e.message,true)}}}
async function users(){const data=await api('/users');const companies=await api('/companies');$('view').innerHTML=`<div class="panel"><h2>Crear cuenta</h2><form id="user-form" class="box-form"><label>Usuario<input name="username" required></label><label>Contraseña inicial<input name="password" type="password" minlength="12" required></label><label>Rol<select name="role">${['externo','tecnico','supervisor','coordinador','gerencia','administrador'].map(x=>`<option>${x}</option>`).join('')}</select></label><label>Empresa (externo)<select name="company_id"><option value="">Ninguna</option>${companies.filter(x=>x.active).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select></label><label>Nivel (técnico)<select name="level"><option value="">—</option><option>L1</option><option>L2</option><option>L3</option></select></label><button class="primary">Crear</button></form></div><div class="panel" style="margin-top:20px"><h2>Cuentas</h2>${table(['Usuario','Rol','Nivel','Empresa','Activo'],data.map(x=>[esc(x.username),esc(x.role),esc(x.level),esc(x.company_id),tag(x.active?'Sí':'No')]))}</div>`;$('user-form').onsubmit=async(e)=>{e.preventDefault();let f=Object.fromEntries(new FormData(e.target));f.company_id=f.company_id?Number(f.company_id):null;f.level=f.level||null;try{await api('/users',{method:'POST',body:JSON.stringify(f)});await goto('users');flash('Cuenta creada')}catch(e){flash(e.message,true)}}}
async function shifts(){const data=await api('/shifts');let admin=['supervisor'].includes(me.role);let techs=admin?(await api('/users')).filter(u=>u.role==='tecnico'):[];$('view').innerHTML=`${admin?`<div class="panel"><h2>Configurar turno recurrente</h2><form id="shift-form" class="box-form"><label>Técnico<select name="user_id">${techs.map(t=>`<option value="${t.id}">${esc(t.username)} (${esc(t.level)})</option>`).join('')}</select></label><label>Día<select name="weekday">${['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'].map((d,i)=>`<option value="${i}">${d}</option>`).join('')}</select></label><label>Hora inicio<input type="time" name="start" value="08:00" required></label><label>Hora fin<input type="time" name="end" value="17:00" required></label><button class="primary">Guardar turno</button></form></div>`:''}<div class="panel" style="margin-top:20px"><h2>Turnos registrados</h2>${table(['Técnico ID','Día','Inicio','Fin'],data.map(s=>[esc(s.user_id),['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'][s.weekday],String(Math.floor(s.start_minute/60)).padStart(2,'0')+':'+String(s.start_minute%60).padStart(2,'0'),String(Math.floor(s.end_minute/60)).padStart(2,'0')+':'+String(s.end_minute%60).padStart(2,'0')]))}</div>`;if(admin)$('shift-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target);let min=t=>Number(t.split(':')[0])*60+Number(t.split(':')[1]);try{await api('/shifts',{method:'POST',body:JSON.stringify({user_id:Number(f.get('user_id')),weekday:Number(f.get('weekday')),start_minute:min(f.get('start')),end_minute:min(f.get('end'))})});await goto('shifts');flash('Turno creado')}catch(e){flash(e.message,true)}}}
async function audit(){const data=await api('/audit');$('view').innerHTML=`<div class="panel"><h2>Últimas operaciones</h2>${table(['Fecha','Actor','Acción','Entidad','ID','Detalle'],data.map(x=>[pretty(x.date),esc(x.actor_id),esc(x.action),esc(x.entity),esc(x.entity_id),esc(x.detail)]))}</div>`}
async function issues(){const data=await api('/import-issues');$('view').innerHTML=`<div class="panel"><h2>Registros no procesados</h2>${table(['Archivo','Secuencia','Motivo'],data.map(x=>[esc(x.source),esc(x.seq),esc(x.reason)]))}</div>`}
$('login-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{const result=await api('/login',{method:'POST',body:JSON.stringify(Object.fromEntries(f))});token=result.token;sessionStorage.setItem('soast_token',token);me=await api('/me');showApp()}catch(err){flash(err.message,true)}};
$('logout').onclick=async()=>{try{await api('/logout',{method:'POST'})}catch{}token=null;me=null;sessionStorage.removeItem('soast_token');showLogin()};
$('nav').onclick=e=>{const b=e.target.closest('[data-page]');if(b)goto(b.dataset.page)};
$('view').onclick=async e=>{const t=e.target.closest('[data-ticket]');if(t)try{await detail(t.dataset.ticket)}catch(err){flash(err.message,true)};const c=e.target.closest('[data-cancel]');if(c&&confirm('¿Cancelar esta sesión?'))try{await api('/sessions/'+c.dataset.cancel+'/cancel',{method:'POST'});await goto('sessions');flash('Cancelación registrada; confirme estado externo si corresponde')}catch(err){flash(err.message,true)}};
$('clock').textContent=new Date().toLocaleDateString('es-GT',{dateStyle:'full'});
(async()=>{if(token)try{me=await api('/me');showApp();return}catch{}showLogin()})();