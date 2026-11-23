import {datasetField, feedback, field} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {CLOSING, REVIEW, STATUS} from '../constants/enums.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

function runRequest(data, spec) {
  return {dataset_id:Number(data.get('dataset_id')),name:data.get('name'),
    params:Object.fromEntries(spec.params.map(param=>[param.key,data.get(param.key)]))};
}

function sourceCell(row) {
  const lines = row.sources.map(source => source.received
    ? `${source.channel} · ${source.batch || '已到位'}${source.uploaded_at ? `（${date(source.uploaded_at)} 入库）` : ''}`
    : `${source.channel} · 缺数`);
  const late = (row.late || []).map(item =>
    `${item.channel} · ${item.batch}（${date(item.uploaded_at)} 入库，迟到未纳入）`);
  return el('ul', {class:'method-list'}, [...lines, ...late].map(text => el('li', {}, text)));
}

function resultCell(row) {
  if (row.run_id) {
    return el('span', {class:'activity'}, link('查看月报', `#results/${row.run_id}`),
      pill(STATUS[row.run_status] || row.run_status, row.run_status),
      row.run_review && pill(REVIEW[row.run_review] || row.run_review, row.run_review));
  }
  return row.state === 'incomplete' ? '缺数，未出月报' : '—';
}

function closingPanel(spec, closing, refresh) {
  const error = feedback();
  const save = button('保存关账起月', {type:'submit', class:'primary', 'data-testid':'save-closing'});
  const form = session.canWrite && el('form', {class:'fields', onSubmit:handler(save, error, async event => {
    const data = new FormData(event.target);
    await workspace.saveClosing({start_month:data.get('start_month')});
    await refresh();
  })}, field({key:'start_month', label:spec.closing.start_label, type:'month', help:spec.closing.start_help},
    closing.start_month || undefined), el('div', {class:'actions'}, save), error);
  const months = table([
    {key:'period', label:'月份'},
    {label:'状态', render:row=>pill(CLOSING[row.state] || row.state, row.state)},
    {label:'关账时点（北京）', render:row=>date(row.due_at)},
    {label:'渠道来源', render:sourceCell},
    {label:'结果', render:resultCell},
  ], closing.months, {caption:'每月关账来源和结果', empty:'设置关账起月后，到期的月份会自动逐月补算。'});
  return panel(spec.closing.title, p(spec.closing.help, 'subtle'), form, months);
}

export async function runPage(route, refresh) {
  const {spec,datasets,runs} = workspace;
  const scheduling = spec.scheduled && route.scheduling;
  const closing = scheduling ? null : await workspace.closings();
  const plans = scheduling ? await workspace.schedules() : [];
  const error = feedback();
  const submit = button(`执行${spec.run}`,{type:'submit',class:'primary','data-testid':'start-run'});
  const plan = button('保存定时计划',{type:'button',class:'primary','data-testid':'save-schedule'});
  const scheduleFields = scheduling && session.canReview && el('details',{class:'schedule-fields'},
    el('summary',{},'定时站内分发'),
    p('单次执行锁定所选批次。每日计划读取最新导入的批次，报告日逐日推进。通过发布门槛后进入站内收件箱。','subtle'),
    field({key:'recipient',label:'收件团队',type:'string',required:false,max_length:100}),
    field({key:'first_at',label:'首次执行时间（北京时间）',type:'datetime-local',required:false}),
    el('label',{class:'check'},el('input',{name:'repeat_daily',type:'checkbox'}),'每日同一时间运行'), plan);
  const form = el('form',{class:'fields','data-testid':'run-form',
    onSubmit:handler(submit,error,async event=>{
      const item = await workspace.start(runRequest(new FormData(event.target),spec));
      location.hash = `#results/${item.id}`;
    })}, datasetField(datasets,route.id),field({key:'name',label:'运行名称',type:'string',max_length:120},`样本${spec.run}`),
    el('div',{class:'form-grid'},spec.params.map(param=>field(param))),el('div',{class:'actions'},submit),scheduleFields,error);
  plan.addEventListener('click',handler(plan,error,async()=>{
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    const recipient = data.get('recipient'), start = data.get('first_at');
    if (!recipient?.trim() || !start) throw new Error('请填写收件团队和首次执行时间');
    await workspace.schedule({...runRequest(data,spec),recipient,first_at:`${start}:00+08:00`,repeat_daily:data.has('repeat_daily')});
    await refresh();
  }));
  const runList = table([
    {label:'运行',render:item=>link(item.name,`#results/${item.id}`)},
    {label:'状态',render:item=>pill(STATUS[item.status],item.status)},
    {label:'时间',render:item=>date(item.created_at)},
  ],runs,{caption:'运行记录',empty:'首次运行后将在此显示后台状态。'});
  const scheduleList = scheduling && panel('定时计划',table([
    {key:'name',label:'计划'}, {key:'recipient',label:'收件团队'},
    {label:'下次执行（北京时间）',render:row=>row.enabled ? date(row.next_at) : '—'},
    {key:'next_report_day',label:'下次报告日'},
    {label:'状态',render:row=>row.enabled ? '启用' : '已结束'},
    {label:'操作',render:row=>{
      if (!row.enabled || !session.canReview) return '—';
      const action = button('停止计划',{class:'quiet'});
      action.addEventListener('click',handler(action,error,async()=>{await workspace.disable(row.id);await refresh();}));
      return action;
    }},
  ],plans,{caption:'定时计划',empty:'还未配置定时计划。'}));
  const layout = el('div',{class:'two-col'}, el('div',{class:'wide-panel'},
    panel(spec.run,session.canWrite ? (datasets.length ? form : empty('请先导入数据。','#inputs','前往导入'))
      : p('只读角色可以查看上方业务流程的历史运行。','empty')),
    !scheduling && panel('运行记录',runList),scheduleList),
    el('aside',{class:'sidebar-panel'},panel('计算口径',el('ul',{class:'method-list'},spec.methodology.map(text=>el('li',{},text))))));
  return closing ? el('div',{class:'page-stack'},layout,closingPanel(spec,closing,refresh)) : layout;
}
