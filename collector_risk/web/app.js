/* Интерфейс диспетчера: значения источника вставляем как текст, чтобы не выполнять HTML из данных. */
let forecasts=[],objects=[],current=null,selectedObject=null,csrf='',role='',notifications=[],permissions=new Set();
const $=id=>document.getElementById(id);
const fmt=p=>typeof p==='number'&&Number.isFinite(p)?new Intl.NumberFormat('ru-RU',{style:'percent',maximumFractionDigits:1}).format(p):'—';
const priority={'Активный сигнал':0,'Проверить':1,'Нет свежих данных':2,'Мало данных':3,'Наблюдение':4};
const statusTips={'Активный сигнал':'Тревожная запись на дату наблюдения; причина не подтверждена.',
 'Проверить':'Прогноз нового сигнала выше порога на следующие сутки.',
 'Нет свежих данных':'Данные не поступили на последнюю дату выгрузки; проверить связь или датчик.',
 'Мало данных':'Недостаточно наблюдений для надежной оценки.',
 'Наблюдение':'Порог не достигнут при имеющихся данных; исправность не гарантирована.'};
/** Создает DOM-элемент с безопасным текстом и необязательным CSS-классом. */
function node(tag,text='',cls=''){const x=document.createElement(tag);x.textContent=String(text??'');x.className=cls;return x}
/** Вызывает локальный API; для записи передает JSON и CSRF-токен. */
async function api(path,body){const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)};
 const res=await fetch(path,options),out=await res.json();if(!res.ok)throw Error(out.error||'HTTP '+res.status);return out}
/** Проверяет выданное сервером разрешение текущего пользователя. */
function has(permission){return permissions.has(permission)}

/** Форматирует календарную дату источника без придуманного времени и интервал прогноза с часами и минутами. */
function formatDate(value,withTime=false){const raw=String(value??'');const match=/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(raw);
 if(!match)return raw;return `${match[3]}.${match[2]}.${match[1]}${withTime&&match[4]?' '+match[4]+':'+match[5]:''}`}

/** Переключает страницу и запускает ее актуализацию при открытии. */
function page(name){const labels={dashboard:'Прогнозы по объектам',stream:'Поступление данных',map:'Схема площадок',tree:'Объекты и оборудование',reports:'Отчеты',roles:'Роли и доступ'};
 for(const el of document.querySelectorAll('.page'))el.hidden=el.id!==name+'-page';
 for(const el of document.querySelectorAll('[data-page]'))el.classList.toggle('selected',el.dataset.page===name);
 $('page-title').textContent=labels[name];if(name==='map')renderMap();if(name==='tree')renderTree();if(name==='reports'){updateReportHelp();runReport()}if(name==='roles')loadRoles()}

/** Открывает карточку объекта с прогнозом, решением и отдельным черновиком заявки. */
function openDetail(r){current=r;$('drawer-title').textContent=r.object_name;$('detail').replaceChildren();
 // В заголовке и строках карточки нет декоративных точек-разделителей.
 $('detail').append(node('p','Система: '+r.category_ru,'detail-system'));
 const card=node('div','','detail-card');for(const [k,v] of [['Объект ID',r.object_id],['День наблюдения',formatDate(r.date)],['Действует с',formatDate(r.valid_from,true)],['Действует до',formatDate(r.valid_to,true)],
  ['Вероятность нового тревожного дня',fmt(r.score)],['Статус',r.status],['Покрытие',r.confidence],['Сигналы / записи на дату',r.alarms+' / '+r.readings],
  ['Тревожные записи за 7 календарных дней',r.alarms_7d],['Рекомендация',r.action]]){
  const p=node('p');p.append(node('b',k+': '),document.createTextNode(String(v)));card.append(p)}$('detail').append(card);
 $('decision-section').hidden=!has('decisions:write');$('ticket-section').hidden=!has('tickets:write');
 $('ticket-object').value=r.object_id;$('ticket-reason').value=r.action;$('ticket-priority').value=r.status==='Активный сигнал'?'критический':'обычный';
 $('ticket-due').value=new Date(Date.now()+86400000).toISOString().slice(0,10);$('ticket-confirm').hidden=true;
 $('ticket-status').textContent='';$('message').textContent='';$('overlay').hidden=false;$('close').focus()}

/** Применяет фильтры и сортирует прогнозы по срочности перед отрисовкой таблицы. */
function renderForecasts(){const q=$('search').value.trim().toLowerCase(),oid=$('object-filter').value,t=$('type').value,s=$('status').value;
 const min=$('min-score').value===''?null:Number($('min-score').value)/100;
 const rows=forecasts.filter(r=>(!oid||String(r.object_id)===oid)&&(!t||r.category===t)&&(!s||r.status===s)
  &&(min===null||(typeof r.score==='number'&&r.score>=min))&&(!q||String(r.object_id).includes(q)||r.object_name.toLowerCase().includes(q)))
  .sort((a,b)=>(priority[a.status]??9)-(priority[b.status]??9)||(typeof b.score==='number'?b.score:-1)-(typeof a.score==='number'?a.score:-1)||a.object_name.localeCompare(b.object_name,'ru'));
 $('rows').replaceChildren();for(const r of rows.slice(0,300)){const tr=node('tr');tr.tabIndex=0;const name=node('td',r.object_name,'obj');name.append(node('div','ID '+r.object_id,'sub'));
  const badge=node('span',r.status,'badge status-'+(priority[r.status]??4));badge.title=statusTips[r.status]||r.status;
  const td=node('td');td.append(badge);tr.append(name,node('td',r.category_ru),node('td',fmt(r.score),'score'),node('td',r.alarms_7d),td,node('td','→'));
  tr.onclick=()=>openDetail(r);tr.onkeydown=e=>{if(e.key==='Enter')openDetail(r)};$('rows').append(tr)}
 $('count').textContent=`Показано ${Math.min(rows.length,300)} из ${rows.length} записей`}

/** Строит краткую сводку по каждой инженерной системе и объясняет счетчики. */
function renderSystemSummary(){const box=$('system-summary');box.replaceChildren();const types=[...new Map(forecasts.map(r=>[r.category,r.category_ru])).entries()].sort((a,b)=>a[1].localeCompare(b[1],'ru'));
 for(const [key,label] of types){const group=forecasts.filter(r=>r.category===key);const alert=group.filter(r=>r.status==='Активный сигнал').length;
  const predicted=group.filter(r=>r.status==='Проверить').length;const gaps=group.filter(r=>['Нет свежих данных','Мало данных'].includes(r.status)).length;
  const card=node('button','','system-card');card.append(node('strong',label),node('small',`${group.length} объектов`));
  for(const [text,count,style,hint] of [
   ['Тревожный сигнал',alert,'red-dot','У датчика есть тревожная запись; причина не подтверждена.'],
   ['Прогноз выше порога',predicted,'orange-dot','Модель советует проверить объект в следующие сутки.'],
   ['Проблема с данными',gaps,'gray-dot','Нет свежих записей либо наблюдений недостаточно.']]){
    const row=node('span',`${text}: ${count}`,'system-metric '+style);row.title=hint;card.append(row)}
  card.onclick=()=>{$('type').value=key;renderForecasts();$('rows').scrollIntoView({block:'start',behavior:'smooth'})};box.append(card)}}
/** Загружает прогнозы и заполняет фильтры и показатели. */
async function loadForecasts(){forecasts=await api('/api/forecasts');
 $('objects').textContent=new Set(forecasts.map(r=>r.object_id)).size;$('systems-count').textContent=new Set(forecasts.map(r=>r.category)).size;
 $('data-gap').textContent=forecasts.filter(r=>['Нет свежих данных','Мало данных'].includes(r.status)).length;
 const types=[...new Map(forecasts.map(r=>[r.category,r.category_ru])).entries()].sort((a,b)=>a[1].localeCompare(b[1],'ru'));
 $('type').replaceChildren(new Option('Все системы',''),...types.map(([k,v])=>new Option(v,k)));
 $('report-system').replaceChildren(new Option('Все системы',''),...types.map(([k,v])=>new Option(v,k)));
 const names=[...new Map(forecasts.map(r=>[r.object_id,r.object_name])).entries()].sort((a,b)=>a[1].localeCompare(b[1],'ru'));
 $('object-filter').replaceChildren(new Option('Все объекты',''),...names.map(([id,name])=>new Option(`${name} · ${id}`,id)));
 renderSystemSummary();renderForecasts()}
/** Загружает реестр и доступные категории инженерных систем. */
async function loadObjects(){objects=await api('/api/objects');const categories=[...new Set(objects.flatMap(o=>o.categories))].sort();
 $('map-category').replaceChildren(new Option('Все системы',''),...categories.map(v=>new Option(v,v)));renderTree();renderMap()}

/** Группирует объекты по родителю для построения дерева. */
function hierarchy(){const byId=new Map(objects.map(o=>[o.object_id,o]));const map=new Map();for(const o of objects){const p=byId.has(o.parent_id)?o.parent_id:null;if(!map.has(p))map.set(p,[]);map.get(p).push(o)}return map}
/** Отображает раскрываемое дерево с поиском по названию и ID. */
function renderTree(){const list=$('tree-list');list.replaceChildren();const q=$('tree-search').value.toLowerCase(),children=hierarchy();
 // При поиске сохраняем и раскрываем родителей найденного узла.
 /** Проверяет совпадение узла либо его потомков с запросом. */
 function matches(o){return !q||String(o.object_id).includes(q)||o.name.toLowerCase().includes(q)||(children.get(o.object_id)||[]).some(matches)}
 /** Создает ветку дерева с кнопкой перехода к оборудованию. */
 function branch(o){if(!matches(o))return null;const kids=(children.get(o.object_id)||[]).filter(matches);
  const detail=node('details','','tree-branch');detail.open=Boolean(q)||o.level===1;
  const summary=node('summary',`${o.name} · ID ${o.object_id}`);summary.title=o.kind;detail.append(summary);
  const row=node('div','','tree-child');const select=node('button',o.level===3?'Показать датчики':'Посмотреть объект','secondary');
  select.onclick=()=>showEquipment(o);row.append(select);detail.append(row);
  for(const child of kids){const nested=branch(child);if(nested)detail.append(nested)}return detail}
 for(const root of children.get(null)||[]){const el=branch(root);if(el)list.append(el)}
 if(!list.childElementCount)list.append(node('p','Объекты не найдены','muted'))}
/** Выбирает объект и скрывает подсказку до выбора; открывает панель датчиков. */
async function showEquipment(o){selectedObject=o;$('equipment-placeholder').hidden=true;$('equipment-panel').hidden=false;
 $('equipment-title').textContent=o.name+' · ID '+o.object_id;$('equipment-add').hidden=!has('equipment:write');
 $('equipment-system').replaceChildren(new Option('Все системы',''),...o.categories.map(v=>new Option(v,v)));await refreshEquipment()}
/** Обновляет список каналов выбранного объекта с фильтрами. */
async function refreshEquipment(){if(!selectedObject)return;const q=new URLSearchParams({object_id:selectedObject.object_id,q:$('equipment-search').value,system:$('equipment-system').value});
 try{const data=await api('/api/equipment?'+q);
  $('equipment-count').textContent=`Показано ${data.items.length} из ${data.matched_total} совпадающих каналов и локальных записей. За раз выводится не более 200.`;
  $('equipment-list').replaceChildren(...data.items.map(x=>node('div',`${x.system_name} · ${x.sensor_type} · ${x.name} ${x.channel_id?'· канал '+x.channel_id:'· добавлено локально'}`,'event')));
  if(!data.items.length)$('equipment-list').append(node('p','По запросу ничего не найдено','muted'))
 }catch(e){$('equipment-count').textContent=e.message}}

/** Определяет наиболее важный цвет узла на условной схеме. */
function mapState(o){const values=forecasts.filter(r=>r.object_id===o.object_id);
 if(values.some(r=>r.status==='Активный сигнал'))return 'alarm';if(values.some(r=>r.status==='Проверить'))return 'risk';
 if(!values.length||values.some(r=>['Нет свежих данных','Мало данных'].includes(r.status)))return 'missing';return 'normal'}
/** Рисует план-схему: объекты одного родителя рядом, участки ПК — по номеру, без вымышленных координат. */
function renderMap(){const target=$('map-canvas');target.replaceChildren();if(!objects.length)return;
 const selected=$('map-category').value,risk=$('map-risk').value;
 const eligible=objects.filter(o=>o.level===3&&(!selected||o.categories.includes(selected)));
 const visible=eligible.filter(o=>!risk||mapState(o)===risk);
 const parentById=new Map(objects.filter(o=>o.level===2).map(o=>[o.object_id,o]));
 const groups=new Map();for(const o of visible){if(!groups.has(o.parent_id))groups.set(o.parent_id,[]);groups.get(o.parent_id).push(o)}
 const sites=[...groups].sort((a,b)=>(parentById.get(a[0])?.name||'').localeCompare(parentById.get(b[0])?.name||'','ru'));
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('role','img');
 svg.setAttribute('aria-label','Объекты сгруппированы по родительским площадкам; географические координаты неизвестны');
 svg.setAttribute('viewBox',`0 0 1260 ${Math.max(245,20+Math.ceil(sites.length/4)*235)}`);
 /** Создает SVG-элемент с набором атрибутов, чтобы подписи и узлы оставались доступными. */
 function element(tag,attributes={}){const el=document.createElementNS(svg.namespaceURI,tag);for(const [key,value] of Object.entries(attributes))el.setAttribute(key,value);return el}
 for(const [index,[parentId,children]] of sites.entries()){
  const x=16+(index%4)*310,y=14+Math.floor(index/4)*235,parent=parentById.get(parentId);
  svg.append(element('rect',{x,y,width:290,height:218,rx:12,fill:'#fff',stroke:'#d5e5e2'}));
  const heading=element('text',{x:x+14,y:y+29,fill:'#20414a','font-size':15,'font-weight':700});heading.textContent=(parent?.name||`Группа ${parentId}`).slice(0,29);svg.append(heading);
  const separator=element('line',{x1:x+14,y1:y+41,x2:x+276,y2:y+41,stroke:'#dce9e6'});svg.append(separator);
  // Номер ПК дает порядок только внутри одной площадки; остальные элементы упорядочены по ID.
  children.sort((a,b)=>{const station=o=>{const found=String(o.name).match(/ПК\s*(\d+)/i);return found?Number(found[1]):Infinity};return station(a)-station(b)||a.object_id-b.object_id});
  for(const [slot,o] of children.entries()){
   const cx=x+24+(slot%2)*141,cy=y+66+Math.floor(slot/2)*28,state=mapState(o);
   const color={alarm:'#d95648',risk:'#e6a331',normal:'#25a777',missing:'#8d9da5'}[state];
   const g=element('g',{tabindex:0,role:'button','aria-label':`${o.name}, ID ${o.object_id}`});
   g.append(element('circle',{cx,cy,r:8,fill:color}));
   const label=element('text',{x:cx+14,y:cy+5,fill:'#244752','font-size':13});label.textContent=`ID ${o.object_id}`;g.append(label);
   const title=element('title');title.textContent=`${parent?.name||'Площадка'}: ${o.name}, ID ${o.object_id}. ${{alarm:'Есть тревожный сигнал',risk:'Прогноз выше порога',normal:'Наблюдение',missing:'Недостаточно данных'}[state]}`;g.append(title);
   g.onclick=()=>{const records=forecasts.filter(r=>r.object_id===o.object_id).sort((a,b)=>(priority[a.status]??9)-(priority[b.status]??9));
    if(records.length)openDetail(records[0]);else{page('tree');showEquipment(o)}};
   g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();g.click()}};svg.append(g)
  }
 }target.append(svg);
 $('map-count').textContent=`Показано ${visible.length} из ${eligible.length} объектов. Рамка объединяет элементы одной площадки; цвет показывает наиболее важный статус объекта по всем системам. Выберите элемент для карточки.`}

/** Показывает доставленные оповещения и состояние просмотра. */
async function refreshNotifications(){if(!has('notifications:read'))return;notifications=await api('/api/notifications');const list=$('notification-list');list.replaceChildren();
 if(!notifications.length)list.append(node('p','Пока нет оповещений. Запустите демонстрацию на странице «Поступление данных».','muted'));
 for(const n of notifications.slice(0,20)){const row=node('div',`${n.priority==='critical'?'●':'◌'} ${n.title} · объект ${n.object_id} · день источника ${n.source_day}`,'notification');
  if(!n.acknowledged_at&&has('notifications:ack')){const b=node('button','Отметить как просмотренное','secondary');b.onclick=async()=>{await api(`/api/notifications/${n.id}/ack`,{});refreshNotifications()};row.append(b)}
  else row.append(node('small',`Просмотрено ${n.acknowledged_at}`));list.append(row)}}
/** Показывает администратору прогресс исторического воспроизведения. */
async function refreshStream(){if(role!=='admin')return;const s=await api('/api/stream');
 $('stream-state').textContent=`Воспроизведено ${s.cursor} из ${s.total} записей · последний день источника ${s.source_watermark||'—'} · обработано ${s.last_processed_at||'—'} · ошибки ${s.failures}`;
 const events=await api('/api/stream/events');$('stream-events').replaceChildren(...events.slice(0,15).map(e=>node('div',
  `Исходная дата: ${e.source_at.slice(0,10)} · демо-прием: ${e.received_at} · объект ${e.object_id} · система ${e.category} · тревожных записей ${e.alarms}`,'event')))}

const reportHelp={management:'Сводка решений, оповещений, заявок и исторических метрик модели. Объект ограничивает локальные записи.',
 forecast:'Исторический снимок прогноза. Даты не превращают снимок в прогноз на прошлый период; работают фильтры объекта, системы, статуса и порога.',
 activity:'Локальные оповещения, решения и заявки за даты создания; работают фильтры объекта и системы, когда категория известна.',
 quality:'Достаточность и свежесть данных в историческом снимке. Фильтры объекта, системы и статуса.',
 audit:'Журнал входа, чтения, записи и отказов за период. Фильтр объекта применяется к записям с object_id.'};
/** Объясняет назначение выбранного отчета и скрывает неприменимые поля. */
function updateReportHelp(){const kind=$('report-kind').value;$('report-help').textContent=reportHelp[kind];
 const enabled={management:['report-from','report-to','report-object'],forecast:['report-object','report-system','report-status','report-min-score'],
  activity:['report-from','report-to','report-object','report-system'],quality:['report-object','report-system','report-status'],
  audit:['report-from','report-to','report-object']}[kind];
 for(const id of ['report-from','report-to','report-object','report-system','report-status','report-min-score']){
  const field=$(id);field.parentElement.hidden=!enabled.includes(id);
  if(!enabled.includes(id)&&!['report-from','report-to'].includes(id))field.value=''}}
/** Собирает параметры отчета для экрана и обоих форматов выгрузки. */
function reportQuery(){const p=new URLSearchParams({kind:$('report-kind').value,from:$('report-from').value,to:$('report-to').value});
 for(const [key,id] of [['object_id','report-object'],['system','report-system'],['status','report-status'],['min_score','report-min-score']])if($(id).value)p.set(key,$(id).value);
 return '?'+p.toString()}
/** Формирует предпросмотр отчета с теми же фильтрами, что при экспорте. */
async function runReport(){try{const r=await api('/api/reports'+reportQuery()),out=$('report-output');out.replaceChildren(node('h3',r.title));
  for(const note of r.notes||[])out.append(node('p',note,'muted'));
  for(const [key,value] of Object.entries(r.summary||{}))out.append(node('p',`${key}: ${value}`));
  const wrap=node('div','','table-wrap'),table=node('table');const thead=node('thead'),header=node('tr');for(const h of r.columns)header.append(node('th',h));thead.append(header);table.append(thead);
  const tbody=node('tbody');for(const row of r.rows.slice(0,300)){const tr=node('tr');for(const value of row)tr.append(node('td',value));tbody.append(tr)}table.append(tbody);wrap.append(table);out.append(wrap);
  if(r.rows.length>300)out.append(node('p',`На экране первые 300 из ${r.rows.length}; экспорт содержит весь набор.`,'muted'));
 }catch(e){$('report-output').textContent=e.message}}
/** Скачивает PDF или XLSX и освобождает временный URL браузера. */
async function downloadReport(ext){const res=await fetch('/api/reports.'+ext+reportQuery());if(!res.ok){alert((await res.json()).error);return}
 const url=URL.createObjectURL(await res.blob()),a=document.createElement('a');a.href=url;a.download='collector-'+$('report-kind').value+'.'+ext;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000)}

/** Загружает доступные системы для фильтра отчета. */
async function loadReportOptions(){const data=await api('/api/report/options');$('report-system').replaceChildren(new Option('Все системы',''),
 ...data.systems.map(s=>new Option(s.title,s.id)))}

/** Показывает роли, права и пользователей в разделе администратора. */
async function loadRoles(){if(!has('roles:manage'))return;try{const data=await api('/api/roles');
 const list=$('roles-list');list.replaceChildren();$('permission-list').replaceChildren();
 const reserved=new Set(['roles:manage','stream:write','objects:write','registry:sync','audit:read']);
 for(const [key,label] of Object.entries(data.permissions)){
  const item=node('label','','permission-item'),input=document.createElement('input');input.type='checkbox';input.value=key;input.disabled=reserved.has(key);
  item.title=input.disabled?'Доступно только системному администратору':'';
  item.append(input,document.createTextNode(label+(input.disabled?' (только администратор)':'')));$('permission-list').append(item)}
 for(const r of data.roles){const item=node('div','','role-item');item.append(node('b',r.title+' · '+r.role_id),node('small',r.system?'Системная роль; не редактируется':`${r.permissions.length} прав`));
  if(!r.system){const b=node('button','Изменить','secondary');b.onclick=()=>{$('role-id').value=r.role_id;$('role-title').value=r.title;
   for(const x of $('permission-list').querySelectorAll('input'))x.checked=r.permissions.includes(x.value);$('role-form').scrollIntoView({behavior:'smooth'})};item.append(b)}list.append(item)}
 $('new-user-role').replaceChildren(...data.roles.filter(r=>!r.system).map(r=>new Option(r.title,r.role_id)));
 $('users-list').replaceChildren(...data.users.map(u=>node('div',`${u.user_id} · ${u.role_id}`,'role-item')));
 }catch(e){$('role-message').textContent=e.message}}

/** Получает сессию и права с сервера, скрывает недоступные страницы и загружает данные. */
async function bootstrap(){try{const me=await api('/api/me');if(!me.user_id){$('login-page').hidden=false;$('application').hidden=true;return}
 csrf=me.csrf;role=me.role;permissions=new Set(me.permissions);$('identity').textContent={dispatcher:'Диспетчер',admin:'Администратор'}[role]||role;
 $('login-page').hidden=true;$('application').hidden=false;
 const pageRight={dashboard:'forecasts:read',map:'forecasts:read',tree:'objects:read',reports:'reports:read',roles:'roles:manage'};
 for(const item of document.querySelectorAll('[data-page]'))item.hidden=item.dataset.page==='stream'?role!=='admin':!has(pageRight[item.dataset.page]);
 $('object-add').hidden=!has('objects:write');$('registry-sync').hidden=!has('registry:sync');
 for(const id of ['stream-start','stream-step','stream-pause'])$(id).hidden=role!=='admin';
 const jobs=[];if(has('forecasts:read'))jobs.push(loadForecasts());if(has('objects:read'))jobs.push(loadObjects());
 if(has('reports:read'))jobs.push(loadReportOptions());if(has('notifications:read'))jobs.push(refreshNotifications());if(role==='admin')jobs.push(refreshStream());
 await Promise.all(jobs);
 if(has('forecasts:read'))page('dashboard');else if(has('reports:read'))page('reports');else if(has('objects:read'))page('tree');else if(has('roles:manage'))page('roles');
 else {$('notice').hidden=false;$('notice').textContent='Для этой роли пока не назначено страниц. Обратитесь к администратору.'}
 }catch(e){$('notice').hidden=false;$('notice').textContent='Ошибка загрузки: '+e.message}}

// Подключаем фильтры и переходы между рабочими разделами.
for(const id of ['search','object-filter','type','status','min-score'])$(id).addEventListener(['search','min-score'].includes(id)?'input':'change',renderForecasts);
for(const id of ['map-category','map-risk'])$(id).onchange=renderMap;
$('tree-search').oninput=renderTree;$('tree-expand').onclick=()=>document.querySelectorAll('.tree-branch').forEach(x=>x.open=true);
$('tree-collapse').onclick=()=>document.querySelectorAll('.tree-branch').forEach(x=>x.open=false);
$('equipment-search').oninput=()=>refreshEquipment();$('equipment-system').onchange=()=>refreshEquipment();
for(const b of document.querySelectorAll('[data-page]'))b.onclick=()=>page(b.dataset.page);
$('close').onclick=$('shade').onclick=()=>{$('overlay').hidden=true};document.addEventListener('keydown',e=>{if(e.key==='Escape')$('overlay').hidden=true});
// Формы входа и рабочих действий отправляют данные на сервер и показывают результат рядом с действием.
$('login-form').onsubmit=async e=>{e.preventDefault();try{const x=await api('/api/login',{user:$('login-user').value,password:$('login-password').value});csrf=x.csrf;bootstrap()}catch(err){$('login-message').textContent=err.message}};
$('logout').onclick=async()=>{await api('/api/logout',{});role='';location.reload()};
$('decision').onsubmit=async e=>{e.preventDefault();try{const r=await api('/api/decisions',{object_id:current.object_id,category:current.category,decision:$('action').value,reason:$('reason').value});$('message').textContent='Решение №'+r.id+' сохранено'}catch(err){$('message').textContent=err.message}};
$('ticket-form').onsubmit=async e=>{e.preventDefault();try{const r=await api('/api/tickets/drafts',{object_id:current.object_id,equipment:$('equipment').value,reason:$('ticket-reason').value,priority:$('ticket-priority').value,due_at:$('ticket-due').value});$('ticket-id').value=r.id;$('ticket-confirm').hidden=false;$('ticket-status').textContent='Черновик №'+r.id+' сохранен локально. Ожидает подтверждения.'}catch(err){$('ticket-status').textContent=err.message}};
$('ticket-confirm').onclick=async()=>{if(!confirm('Подтвердить локальный черновик? Внешняя заявка не отправляется.'))return;try{const r=await api(`/api/tickets/${$('ticket-id').value}/confirm`,{confirm:true});$('ticket-status').textContent='Подтвержден локально. Внешняя отправка: '+r.external_sent;$('ticket-confirm').hidden=true}catch(err){$('ticket-status').textContent=err.message}};
$('ticket-check').onclick=async()=>{try{const r=await api('/api/tickets/'+encodeURIComponent($('ticket-id').value));$('ticket-status').textContent=`Внешний статус: ${r.external_status}. ${r.source}. Локальный черновик: ${r.local_draft?.status||'не найдено'}.`}catch(err){$('ticket-status').textContent=err.message}};
$('object-form').onsubmit=async e=>{e.preventDefault();try{const r=await api('/api/objects',{parent_id:+$('parent-id').value,name:$('new-name').value});$('object-message').textContent='Добавлен локальный ID '+r.object_id;await loadObjects()}catch(err){$('object-message').textContent=err.message}};
$('equipment-form').onsubmit=async e=>{e.preventDefault();try{const r=await api('/api/equipment',{object_id:selectedObject.object_id,system_name:$('eq-system').value,sensor_type:$('eq-type').value,name:$('eq-name').value});$('equipment-message').textContent='Добавлено локально №'+r.id;await refreshEquipment()}catch(err){$('equipment-message').textContent=err.message}};
$('registry-sync').onclick=async()=>{try{const r=await api('/api/registry/sync',{});$('object-message').textContent=`Новые ${r.added}, изменены ${r.updated}, отсутствуют ${r.missing}`;await loadObjects()}catch(err){$('object-message').textContent=err.message}};
// Изменение прав и учетных записей доступно только после проверки серверной роли администратора.
$('role-form').onsubmit=async e=>{e.preventDefault();try{const rights=[...$('permission-list').querySelectorAll('input:checked')].map(x=>x.value);
 const r=await api('/api/roles',{role_id:$('role-id').value,title:$('role-title').value,permissions:rights});
 $('role-message').textContent='Сохранена роль '+r.title;await loadRoles()}catch(err){$('role-message').textContent=err.message}};
$('user-form').onsubmit=async e=>{e.preventDefault();try{const r=await api('/api/users',{user_id:$('new-user-id').value,role_id:$('new-user-role').value,password:$('new-user-password').value});
 $('user-message').textContent='Сохранена учетная запись '+r.user_id;$('new-user-password').value='';await loadRoles()}catch(err){$('user-message').textContent=err.message}};
for(const [id,command] of [['stream-start','start'],['stream-step','step'],['stream-pause','pause']])$(id).onclick=async()=>{try{await api('/api/stream/control',{command,count:10,speed:4});refreshStream();refreshNotifications()}catch(e){$('stream-state').textContent=e.message}};
$('report-kind').onchange=updateReportHelp;$('report-run').onclick=runReport;$('report-xlsx').onclick=()=>downloadReport('xlsx');$('report-pdf').onclick=()=>downloadReport('pdf');
const today=new Date().toISOString().slice(0,10);$('report-to').value=today;$('report-from').value=new Date(Date.now()-30*86400000).toISOString().slice(0,10);
// Периодическое обновление показывает новые оповещения без перезагрузки страницы.
setInterval(()=>{if(has('notifications:read'))refreshNotifications().catch(()=>{});if(role==='admin')refreshStream().catch(()=>{})},5000);bootstrap();
