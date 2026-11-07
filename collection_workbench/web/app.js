'use strict';
const $ = id => document.getElementById(id);
const clone = value => JSON.parse(JSON.stringify(value));
const localToken = document.querySelector('meta[name="workbench-token"]').content;
const state = {config:null, labelsById:new Map(), planById:new Map(), capture:null, mode:'replay', session:null,
  labels:[], baseline:[], undo:[], dirty:false, formDirty:false, editing:null, frame:null,
  frameN:0, key:null, baseRevision:null, playing:false, download:null, captureBusy:false, library:[], events:[], eventId:null};
let frameRequest = 0, frameAbort = null, playEpoch = null, pollBusy = false;

function message(text, error=false) {
  $('message').textContent=text; $('message').hidden=!text;
  $('message').classList.toggle('error',error);
}
async function api(url, data, options={}) {
  const response=await fetch(url,{method:data===undefined?'GET':'POST',cache:'no-store',
    headers:{'X-Workbench-Token':localToken,...(data===undefined?{}:{'Content-Type':'application/json'})},
    ...(data===undefined?{}:{body:JSON.stringify(data)}),...options});
  if(!response.ok){const body=await response.json().catch(()=>({error:'服务请求失败'}));throw new Error(body.error);}
  return response.json();
}
function handle(fn){return async event=>{try{await fn(event);}catch(error){if(error.name!=='AbortError')message(error.message,true);}};}
function labelName(id){return state.labelsById.get(id)?.name||`${id}（规则外标签，需核对）`;}
function parsedContext(row){try{return row?.capture_context?JSON.parse(row.capture_context):null;}catch{return null;}}
function contextSummary(row){
  if(!row?.capture_context)return '';
  try{const data=parsedContext(row),schema=state.config.targeted_schema;if(!data)throw new Error('invalid context');
    const motion=schema.motion_directions.find(item=>item.id===data.motion_direction)?.name||'未记录';
    const visibility=schema.visibility_options.find(item=>item.id===data.visibility)?.name||'未记录';
    return `${data.body_angle_deg}° · 第${data.cycle_index}次周期 · 方向：${motion} · 轮廓：${visibility}`;
  }catch{return '采集条件无法解析，保留原记录。';}
}
function setMode(mode){
  if(mode!==state.mode)message('');
  state.mode=mode; $('capture-panel').hidden=mode!=='capture'; $('replay-panel').hidden=mode!=='replay';
  $('events-panel').hidden=mode!=='events';
  document.body.classList.toggle('capture-running',mode==='capture'&&['starting','recording','stopping'].includes(state.capture?.state));
  for(const name of ['capture','replay','events'])$('tab-'+name).setAttribute('aria-selected',String(mode===name));
  if(mode!=='replay')stopPlay();
}
function fillLabels(select,recording=false){
  select.replaceChildren(); let group=null;
  const items=[...state.config.label_schema.labels];
  if(recording)items.sort((a,b)=>Number(state.planById.get(a.id)?.deferred)-Number(state.planById.get(b.id)?.deferred)||(state.planById.get(a.id)?.order??999)-(state.planById.get(b.id)?.order??999));
  for(const item of items){
    const plan=recording?state.planById.get(item.id):null,heading=plan?.deferred?'本计划暂缓（以后可补采）':plan?.stage||item.group;
    if(!group||group.label!==heading){group=document.createElement('optgroup');group.label=heading;select.append(group);}
    const option=document.createElement('option');option.value=item.id;
    option.textContent=plan?`${plan.deferred?'暂缓':String(plan.order).padStart(2,'0')} ${item.name}｜${plan.count}｜${plan.duration}`:item.name;
    option.title=plan?`${plan.count}；${plan.duration}。${plan.description}`:item.definition;
    if(recording){
      const reason=labelUnavailable(item.id),prepare=item.activity_provenance==='controlled_simulation'&&!plan?.deferred&&$('activity-source').value==='everyday_only'&&!captureRunning();
      option.disabled=!!reason&&!prepare;
      if(reason)option.title=prepare?'选择后准备受控模拟采集；开始前需填写实际模拟方式。':reason;
    }
    group.append(option);
  }
}
function captureRunning(){return ['starting','recording','stopping'].includes(state.capture?.state);}
function previewRunning(){return ['starting','previewing','stopping'].includes(state.capture?.preview?.state);}
function targetedMode(){return $('recording-profile').value==='targeted';}
function targetedTarget(){return state.config.targeted_schema.targets.find(item=>item.id===$('targeted-action').value)||state.config.targeted_schema.targets[0];}
function targetedAngles(){return [...$('targeted-angles').querySelectorAll('input:checked')].map(input=>input.value);}
function initTargetedFields(){
  const schema=state.config.targeted_schema;
  for(const [id,items]of [['targeted-action',schema.targets],['targeted-motion',schema.motion_directions],['targeted-visibility',schema.visibility_options]]){
    for(const item of items){const option=document.createElement('option');option.value=item.id;option.textContent=item.name;$(id).append(option);}
  }
  for(const item of schema.angles){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=item.id;label.append(input,document.createTextNode(item.name));$('targeted-angles').append(label);}
}
function prepareTargetedMode(){
  if(targetedMode()){
    if($('collection-purpose').value==='device_check')$('collection-purpose').value='functional_trial';
    $('activity-source').value=targetedTarget().controlled?'everyday_and_controlled':'everyday_only';
  }
  syncCollectionFields();
}
function adoptTargetedConfig(config){
  if(!config)return;
  $('targeted-action').value=config.target_id;$('targeted-count').value=String(config.repetitions_per_angle);
  for(const input of $('targeted-angles').querySelectorAll('input'))input.checked=config.angles.includes(input.value);
}
function syncTargetedPanel(){
  const enabled=targetedMode(),running=captureRunning(),capture=state.capture;
  document.body.classList.toggle('targeted-running',enabled&&running);
  $('targeted-setup').hidden=!enabled;
  $('targeted-setup').disabled=!enabled||!capture?.enabled||running||state.captureBusy;
  for(const id of ['targeted-live','targeted-stage-guide','targeted-conditions','targeted-phase-title'])$(id).hidden=!enabled;
  $('live-label-field').hidden=enabled;
  $('targeted-count').required=enabled;
  $('live-label-title').textContent=enabled?'本次当前阶段':'全部动作（按录制顺序 · 次数为本场计划）';
  $('live-label').disabled=enabled;
  $('label-plan').hidden=enabled;$('label-definition').hidden=enabled;
  const target=targetedTarget(),angles=targetedAngles(),count=Number($('targeted-count').value);
  const validCount=Number.isInteger(count)&&count>=1&&count<=100;
  $('targeted-plan-hint').textContent=validCount?`已选${angles.length}个角度，每角度${count}次，计划共${angles.length*count}个完整周期。完成整个周期才计1次，可随时停止。`:'每角度次数请填写1到100的整数。';
  const progress=enabled&&running?capture?.targeted:null;
  const actualAngles=progress?.config.angles||angles,select=$('targeted-live-angle'),previous=select.value;
  if([...select.options].map(option=>option.value).join(',')!==actualAngles.join(',')){
    select.replaceChildren();
    for(const angle of actualAngles){const item=state.config.targeted_schema.angles.find(row=>row.id===angle),option=document.createElement('option');option.value=angle;option.textContent=item.name;select.append(option);}
    if(actualAngles.includes(previous))select.value=previous;
  }
  if(progress?.cycle)select.value=progress.cycle.body_angle_deg;
  select.disabled=!!progress?.cycle||!!capture?.active||!running||state.captureBusy;
  $('targeted-next-angle').disabled=select.disabled||actualAngles.length<2;
  for(const id of ['targeted-motion','targeted-visibility'])$(id).disabled=!running||!!capture?.active||state.captureBusy;
  $('targeted-reset').disabled=!running||!progress?.cycle||!!capture?.active||state.captureBusy;
  const current=progress?.expected_phase||target.phases[0];
  if(enabled){
    chooseLiveLabel(current.label);
    $('label-start').textContent='开始'+current.name;$('label-end').textContent='结束'+current.name;
    const stageIndex=progress?.phase_index??0;
    $('targeted-phase-title').textContent=`阶段${stageIndex+1}/${target.phases.length}：${current.name}`;
    $('targeted-stage-guide').textContent=`阶段${stageIndex+1}/${target.phases.length}：${current.instruction}`;
    const previousCompleted=!running&&capture?.targeted?`上一会话完成${capture.targeted.completed_cycles}个周期。`:'';
    $('targeted-progress').textContent=progress?actualAngles.map(angle=>`${angle}° ${progress.completed[angle]}/${progress.config.repetitions_per_angle}次`).join(' · ')+`；共完成${progress.completed_cycles}次`:previousCompleted+'按所选角度逐次记录，开始采集后计数。';
  }else{$('label-start').textContent='开始动作';$('label-end').textContent='结束动作';}
  $('targeted-protection-preset').hidden=!enabled||!target.controlled;
}
function labelUnavailable(id){
  if(state.planById.get(id)?.deferred)return '本计划暂缓；需要时结束本场后改用完整参考计划。';
  const source=$('activity-source').value,kind=state.labelsById.get(id)?.activity_provenance;
  if(kind==='controlled_simulation'&&source!=='everyday_and_controlled')return '受控模拟为可选项；开始采集前需声明动作来源及实际模拟方式。';
  if(source==='equipment_only'&&!['empty','occlusion','unknown','invalid'].includes(id))return '仅设备检查不记录人体动作。';
  return '';
}
function syncCollectionFields(){
  const form=$('capture-form'),purpose=$('collection-purpose').value,mixed=$('activity-source').value==='everyday_and_controlled';
  $('installation-id').required=purpose!=='device_check';
  form.elements.namedItem('height_m').required=purpose==='dataset_collection';
  form.elements.namedItem('orientation').required=purpose==='dataset_collection';
  $('simulation-protocol-field').hidden=!mixed;$('simulation-protocol').required=mixed;
  const profile=state.config.label_schema.recording_profiles.find(p=>p.id===$('recording-profile').value);
  const ids=targetedMode()?targetedTarget().phases.map(item=>item.label):profile.label_ids,previous=$('live-label').value;
  state.planById=new Map(state.config.label_schema.recording_plan.map(p=>[p.id,{...p,deferred:!ids.includes(p.id),order:ids.includes(p.id)?ids.indexOf(p.id)+1:p.order}]));
  if(targetedMode())for(const [index,phase]of targetedTarget().phases.entries())state.planById.set(phase.label,{id:phase.label,order:index+1,deferred:false,stage:targetedTarget().name,count:'完整周期内的一个阶段',duration:'按实际完成',description:phase.instruction});
  $('profile-description').textContent=profile.description+(purpose==='dataset_collection'?' 数据集采集须填写实际高度与朝向；条件未知时先选功能试采。':'');
  $('recording-order-note').textContent=profile.order_note;
  fillLabels($('live-label'),true);
  chooseLiveLabel(previous&&!labelUnavailable(previous)?previous:[...$('live-label').options].find(p=>!p.disabled)?.value||'empty');
  syncTargetedPanel();
}
function chooseLiveLabel(value){
  $('live-label').value=value;
  $('label-definition').textContent=state.labelsById.get(value)?.definition||'';
  const plan=state.planById.get(value);
  $('label-plan').textContent=plan?`本场参考：${plan.count}；${plan.duration}。${plan.description}`:'本动作未配置计划次数和时长，按实际情况记录。';
}
function selectLiveLabel(value){
  const controlled=state.labelsById.get(value)?.activity_provenance==='controlled_simulation';
  if(controlled&&$('activity-source').value==='everyday_only'&&!captureRunning()){
    $('activity-source').value='everyday_and_controlled';
    syncCollectionFields();chooseLiveLabel(value);$('capture-setup').open=true;
    message('已准备受控模拟采集。请填写实际模拟方式与保护条件，再点击“开始采集”；每轮分别标记下降、低姿态保持和恢复。');
    $('simulation-protocol').focus();
  }else{
    chooseLiveLabel(value);
    const reason=labelUnavailable(value);if(reason)message(reason,true);
  }
}
function scale(){
  const low=Number($('temp-min').value),high=Number($('temp-max').value);
  if(!Number.isFinite(low)||!Number.isFinite(high)||low>=high)throw new Error('温标需满足最低温度小于最高温度');
  document.querySelectorAll('.scale-low').forEach(el=>el.textContent=low+' °C');
  document.querySelectorAll('.scale-high').forEach(el=>el.textContent=high+' °C');
  return [low,high];
}
const stops=[[0,0,4],[87,15,109],[188,55,84],[249,142,9],[252,255,164]];
function heatColor(t){
  const p=Math.max(0,Math.min(1,t))*4,a=Math.min(3,Math.floor(p)),f=p-a;
  return stops[a].map((v,i)=>Math.round(v*(1-f)+stops[a+1][i]*f));
}
function drawHeat(id,row){
  const canvas=$(id),ctx=canvas.getContext('2d');
  ctx.fillStyle='#202728';ctx.fillRect(0,0,canvas.width,canvas.height);
  if(!row||!Array.isArray(row.grid_c)||row.grid_c.length!==768)return;
  const [low,high]=scale(),small=document.createElement('canvas');small.width=32;small.height=24;
  const sctx=small.getContext('2d'),img=sctx.createImageData(32,24);
  for(let i=0;i<768;i++){
    const valid=Array.isArray(row.valid_mask)&&row.valid_mask[i]===true&&typeof row.grid_c[i]==='number'&&Number.isFinite(row.grid_c[i]);
    const rgb=valid?heatColor((row.grid_c[i]-low)/(high-low)):[102,112,109];
    img.data.set([...rgb,255],i*4);
  }
  sctx.putImageData(img,0,0);ctx.imageSmoothingEnabled=false;ctx.drawImage(small,0,0,canvas.width,canvas.height);
}
function pixelInfo(event,canvas,row,target){
  if(!row?.grid_c)return;
  const r=canvas.getBoundingClientRect(),x=Math.min(31,Math.max(0,Math.floor((event.clientX-r.left)/r.width*32))),
    y=Math.min(23,Math.max(0,Math.floor((event.clientY-r.top)/r.height*24))),i=y*32+x;
  const value=row.valid_mask?.[i]===true&&typeof row.grid_c[i]==='number'?row.grid_c[i].toFixed(2)+' °C':'无效像素';
  $(target).textContent=`第 ${y+1} 行，第 ${x+1} 列：${value}。方向沿用驱动行列顺序。`;
}
function qualityText(row){
  if(!row)return '等待真实数据';
  const names={valid:'有效',warmup:'预热，不用于动作分析',invalid:'无效或像素不完整',error:'读取错误'};
  const status=row.relative&&!row.relative.valid&&row.status==='valid'?'结构或时序异常，不能用于分析':names[row.status]||row.status;
  return `帧 ${row.frame_id} · ${status} · 有效像素 ${row.valid_pixels??'未知'}/768 · 读取 ${Number(row.read_ms||0).toFixed(1)} ms${row.simulated?' · 模拟数据':''}`;
}
function radarText(result){
  if(!result?.available)return '雷达数据不可用或已过期；不据此判断无人或安全。';
  const row=result.row,names={0:'无人',1:'运动目标',2:'静止目标',3:'运动＋静止',4:'底噪检测中',5:'底噪检测完成',6:'底噪检测失败'};
  return `雷达：${names[row.state]??'未定义状态'} · ${row.mode==='engineering'?'工程模式':'基础模式'} · 检测距离 ${row.detect_dist_cm} cm · 运动能量 ${row.moving_energy} · 静止能量 ${row.static_energy}`+(result.age_ms!=null?` · 接收记录相差 ${Number(result.age_ms).toFixed(0)} ms`:'');
}

function showFallTest(preview, running){
  const fall=preview.fall, block=$('fall-test'), button=$('fall-test-toggle'), badge=$('fall-test-status');
  block.hidden=!fall?.available;
  if(block.hidden)return;
  const active=previewRunning(), testing=active&&fall.mode!=='off';
  button.textContent=testing?'结束跌倒测试':'开始跌倒测试';
  button.disabled=running||state.captureBusy||preview.state==='stopping'||(active&&!testing);
  const recent=fall.mode==='monitoring'&&fall.last_candidate!=null&&preview.elapsed_s-fall.last_candidate<=4;
  const labels={off:'未启动',warming:'正在积累有效帧',monitoring:recent?'刚检出疑似跌倒':'当前未触发',
    candidate:'疑似跌倒',stale:'等待有效热像',stopped:'测试已结束',error:'测试异常'};
  const name=preview.state==='stopping'&&testing?'正在结束':labels[fall.mode];
  if(badge.textContent!==name)badge.textContent=name;
  badge.classList.toggle('warning',recent||['candidate','stale','error'].includes(fall.mode));
  const details=[];
  if(fall.score!=null)details.push(`当前分数 ${fall.score.toFixed(3)} / 阈值 ${fall.threshold.toFixed(2)}`);
  if(fall.mode!=='off')details.push(`本轮检出疑似跌倒 ${fall.events} 次`);
  if(fall.last_candidate!=null)details.push(`最近一次在测试开始后 ${fall.last_candidate.toFixed(1)} 秒`);
  $('fall-test-details').textContent=fall.error||details.join(' · ')||'仅测试，候选结果在本页显示。';
}

async function pollCapture(){
  if(pollBusy)return;pollBusy=true;
  try{
    const wasRunning=captureRunning(),wasPreview=previewRunning(),capture=await api('/api/capture');state.capture=capture;
    const preview=capture.preview||{state:'idle'},previewActive=previewRunning();
    const names={idle:'未启动',starting:'初始化',recording:'采集中',stopping:'正在收尾',finished:'已保存',cancelled:'已取消，移入回收区',failed:'采集失败'};
    const previewNames={starting:'预览初始化',previewing:'仅预览，不保存',stopping:'正在关闭预览',stopped:'预览已关闭，未保存',failed:'预览失败'};
    $('capture-status').textContent=!captureRunning()&&preview.state!=='idle'?(previewNames[preview.state]||preview.state):(names[capture.state]||capture.state);
    const running=['starting','recording','stopping'].includes(capture.state);
    if(running&&capture.collection_context){
      const context=capture.collection_context;
      adoptTargetedConfig(context.targeted_collection);
      if($('recording-profile').value!==context.recording_profile||$('activity-source').value!==context.activity_source||$('collection-purpose').value!==context.collection_purpose){
        $('recording-profile').value=context.recording_profile;$('activity-source').value=context.activity_source;$('collection-purpose').value=context.collection_purpose;
        syncCollectionFields();
      }
    }
    if(wasRunning!==running)syncCollectionFields();
    document.body.classList.toggle('capture-running',running&&state.mode==='capture');
    for(const input of $('capture-form').elements)input.disabled=!capture.enabled||running||state.captureBusy;
    $('capture-start').textContent=({starting:'正在初始化…',recording:'采集中',stopping:'正在收尾…'})[capture.state]||'开始采集';
    $('capture-start').disabled=!capture.enabled||running||previewActive||state.captureBusy;
    $('preview-start').disabled=!capture.enabled||running||previewActive||state.captureBusy;
    $('preview-stop').disabled=!['starting','previewing'].includes(preview.state)||state.captureBusy;
    showFallTest(preview,running);
    $('label-start').disabled=capture.state!=='recording'||!!capture.active||state.captureBusy||!!labelUnavailable($('live-label').value);
    $('label-end').disabled=capture.state!=='recording'||!capture.active||state.captureBusy;
    $('note-add').disabled=capture.state!=='recording'||state.captureBusy;
    $('capture-stop').disabled=!['starting','recording'].includes(capture.state)||state.captureBusy;
    $('capture-cancel').disabled=!['starting','recording'].includes(capture.state)||state.captureBusy;
    $('active-label').textContent=capture.active?`正在标记：${labelName(capture.active.label)} · ${capture.active.elapsed_s.toFixed(1)} 秒`:'当前没有进行中的动作';
    $('active-label').classList.toggle('running',!!capture.active);
    const usePreview=!running&&preview.state!=='idle',frame=usePreview?preview.latest:capture.latest;
    $('live-empty').hidden=!!frame;
    if(frame){drawHeat('live-canvas',frame);$('live-quality').textContent=qualityText(frame)+(usePreview?' · 仅预览，未保存':capture.latest_age_s>3?` · 距最近一帧 ${capture.latest_age_s.toFixed(1)} 秒`:'');}
    else{drawHeat('live-canvas',null);$('live-quality').textContent=usePreview?'预览中，等待真实温度帧':'等待真实数据';}
    const counts=capture.counts;
    $('capture-summary').textContent=usePreview?`预览 ${preview.elapsed_s.toFixed(1)} 秒 · 有效帧 ${preview.counts?.valid_frames??0} · 不生成数据集`:
      capture.session_id?`${capture.dataset_name||'本次采集'} · ${capture.elapsed_s.toFixed(1)} 秒 · 已完成 ${capture.labels} 段 · 有效帧 ${counts.valid_frames??0} · 失败 ${counts.failure_frames??0}`:'';
    if(capture.error)$('live-quality').textContent='采集异常：'+capture.error;
    if(usePreview&&preview.error)$('live-quality').textContent='预览异常：'+preview.error;
    if(capture.state==='stopping')$('capture-summary').textContent+=` · 正在收尾 ${Number(capture.stopping_elapsed_s||0).toFixed(1)} 秒`;
    const radar=capture.radar||{state:'disabled'},namesRadar={disabled:'雷达未启用',starting:'雷达初始化',reading:'雷达读取中',stale:'雷达数据过期',error:'雷达异常',stopped:'雷达已停止'};
    $('radar-live-status').textContent=namesRadar[radar.state]||'雷达状态未知';
    $('radar-live-details').textContent=usePreview?'仅预览热像，雷达未启动。':radar.error?'雷达异常：'+radar.error:radar.latest?radarText({available:radar.state==='reading'||radar.state==='stopped',row:radar.latest,age_ms:radar.state==='reading'?radar.age_s*1000:null})+` · 已保存 ${radar.counts?.valid_frames??0} 帧，坏帧候选 ${radar.counts?.invalid_candidates??0}`:radar.state==='disabled'?'本场未启用雷达，不生成替代数据。':'等待雷达真实数据。';
    if(usePreview)$('radar-live-status').textContent='预览不读取雷达';
    if(wasRunning&&!running){
      if(capture.state==='cancelled')message('已取消本次采集，内容已移入回收区，可恢复。');
      else if(capture.state==='finished')message('采集已结束并保存。可在“回放订正”查看数据集。');
      else if(capture.state==='failed')message('采集收尾遇到问题：'+(capture.error||'请检查原始记录'),true);
    }
    if(wasPreview&&!previewActive)message(preview.error?'预览关闭异常：'+preview.error:'预览已关闭，没有保存画面。',!!preview.error);
    syncTargetedPanel();
  }catch(error){
    if(!$('fall-test').hidden){
      $('fall-test-status').textContent='状态连接中断';
      $('fall-test-details').textContent='等待工作台恢复连接。';
      $('fall-test-toggle').disabled=true;
    }
    message('采集状态读取失败：'+error.message,true);
  }finally{pollBusy=false;}
}
async function captureCommand(kind,extra={}){
  if(state.captureBusy)return;
  const sessionId=state.capture?.session_id;if(!sessionId)throw new Error('请先开始采集');
  if(kind==='label_start'&&state.capture.targeted)Object.assign(extra,{body_angle_deg:$('targeted-live-angle').value,motion_direction:$('targeted-motion').value,visibility:$('targeted-visibility').value});
  state.captureBusy=true;
  try{
    for(const id of ['label-start','label-end','capture-stop','capture-cancel','note-add'])$(id).disabled=true;
    await api('/api/capture/'+kind,{session_id:sessionId,request_id:crypto.randomUUID(),...extra});
    const feedback={label_start:'动作开始已记录。',label_end:'动作结束已记录。',note:'现场备注已记录。',stop:'已请求结束，正在收尾保存。',cancel:'已请求取消，收尾后将移入回收区。',targeted_reset:'本次周期已放弃，已录阶段保留，不计完成次数。'};
    message(feedback[kind]);
  }finally{state.captureBusy=false;await pollCapture();}
}

async function previewCommand(kind){
  if(state.captureBusy)return;
  state.captureBusy=true;
  try{
    $('preview-start').disabled=true;$('preview-stop').disabled=true;$('fall-test-toggle').disabled=true;
    await api('/api/preview/'+kind,{});
    message({start:'正在打开热像预览，画面仅在内存显示，不保存数据集。',
      'fall-test':'跌倒测试正在启动，结果将在热像上方显示。',stop:'正在关闭预览。'}[kind]);
  }finally{state.captureBusy=false;await pollCapture();}
}

async function loadTrash(){
  const result=await api('/api/trash'),list=$('trash-list');list.replaceChildren();
  for(const item of result.items){
    const row=document.createElement('div'),description=document.createElement('p'),button=document.createElement('button');
    row.className='trash-row';description.textContent=`${item.display_name||item.session_id} · ${item.reason==='operator_cancel'?'取消录制':'已删除'} · ${item.session_id}`;
    button.type='button';button.textContent='恢复数据集';button.addEventListener('click',handle(async()=>{
      if(state.dirty||state.formDirty)throw new Error('请先保存或放弃当前标签修改，再恢复数据集。');
      button.disabled=true;
      try{await api('/api/trash/restore',{trash_id:item.trash_id});await loadTrash();await scanSessions();message('数据集已恢复，原始记录和标签修订均保留。');}
      finally{button.disabled=false;}
    }));row.append(description,button);list.append(row);
  }
  if(!result.items.length){const p=document.createElement('p');p.className='hint';p.textContent='回收区为空。';list.append(p);}
  if(result.issues.length)message('部分回收记录需核查：'+result.issues.join('；'),true);
}

function sessionDate(session){
  const match=/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z_/.exec(session.session_id||'');
  if(!match)return '未记录';
  const time=new Date(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}Z`);
  if(!Number.isFinite(time.getTime()))return '未记录';
  return new Intl.DateTimeFormat('sv-SE',{year:'numeric',month:'2-digit',day:'2-digit',timeZone:'Asia/Shanghai'}).format(time);
}
function updateSessionDates(){
  const select=$('session-date'),old=select.value,counts=new Map();
  for(const session of state.library){const day=sessionDate(session);counts.set(day,(counts.get(day)||0)+1);}
  select.replaceChildren();
  const all=document.createElement('option');all.value='';all.textContent='全部日期';select.append(all);
  for(const day of [...counts.keys()].sort().reverse()){
    const option=document.createElement('option');option.value=day;option.textContent=`${day}（${counts.get(day)}份）`;select.append(option);
  }
  select.value=counts.has(old)?old:'';
}
function renderSessionOptions(){
  const select=$('session-select'),old=select.value,day=$('session-date').value;
  const sessions=state.library.filter(session=>!day||sessionDate(session)===day);
  select.replaceChildren();
  for(const session of sessions){
    const option=document.createElement('option');option.value=session.key;
    const statusNames={finished:'已结束',interrupted:'异常中断',recording:'采集中',starting:'初始化',stopping:'停止保存中',failed:'失败'};
    const duration=Number.isFinite(session.seconds)?`${(session.seconds/60).toFixed(1)}分钟`:'时长未记录';
    option.textContent=`${session.display_name||session.scene||session.session_id} · ${duration} · ${statusNames[session.status]||session.status} · ${session.session_id.slice(-8)}`;
    option.title=`场景：${session.scene||'未记录'}；参与者：${session.participant||'未记录'}；内部ID：${session.session_id}`;
    select.append(option);
  }
  if(!sessions.length){const option=document.createElement('option');option.value='';option.textContent=state.library.length?'该日期没有会话':'未找到会话';select.append(option);}
  if([...select.options].some(o=>o.value===old))select.value=old;
  $('session-library-count').textContent=`共${state.library.length}份，当前显示${sessions.length}份。按新到旧排列，旧会话可按日期筛选。`;
  $('library-empty').hidden=!!state.library.length;
  $('session-open').disabled=!sessions.length;
  syncRecovery();
}
async function scanSessions(){
  const data=await api('/api/sessions');
  state.library=data.sessions;
  updateSessionDates();renderSessionOptions();
  if(data.issues.length)message('部分会话未载入：'+data.issues.join('；'),true);
}
function syncRecovery(){
  const row=state.library.find(item=>item.key===$('session-select').value);
  $('session-delete').disabled=!row||['starting','recording','stopping'].includes(row.status);
  $('dataset-rename').disabled=!row;$('dataset-display-name').value=row?.display_name||row?.scene||'';
  $('recovery-panel').hidden=!row||!['starting','recording','stopping'].includes(row.status);
}
function dirty(value=true){state.dirty=value;$('dirty-status').textContent=value?'有未保存修改':'尚无未保存修改';}
function snapshot(){state.undo.push(clone(state.labels));if(state.undo.length>30)state.undo.shift();$('label-undo').disabled=false;}
function setFacts(data){
  const session=data.session,meta=data.metadata;
  const purposes={functional_trial:'功能试采',dataset_collection:'数据集采集',device_check:'设备检查'},sources={everyday_only:'仅日常动作',everyday_and_controlled:'日常动作＋受控模拟',equipment_only:'仅设备检查'};
  const pairs=[['数据集名称',session.display_name||session.scene||session.session_id],['内部会话ID',session.session_id],['场景',session.scene||'未记录'],['参与者',session.participant||'未记录'],
    ['会话状态',({finished:'已结束',interrupted:'异常中断，需人工核对',failed:'失败'})[session.status]||session.status],
    ['来源',meta.thermal_simulated?'模拟热像，不能作真实采样':'真实热像记录'],
    ['采集目的',purposes[meta.collection_purpose]||'历史记录未填写'],
    ['动作来源',sources[meta.activity_source]||'历史记录未填写；不能根据真实热像推断动作来源'],
    ['采集计划',meta.recording_profile_snapshot?.name||state.config.label_schema.recording_profiles.find(p=>p.id===meta.recording_profile)?.name||'历史记录未填写'],
    ['安装配置',meta.installation?.configuration_id||'未记录'],
    ['数据划分',meta.dataset_assignment==='unassigned'?'尚未划分；不自动成为训练或测试集':'本工具未记录划分'],
    ['雷达',session.radar_mode==='not_connected'?'未接入':session.radar_mode==='simulated_only'?'仅模拟，不能用于融合训练':session.radar_mode],
    ['会话跨度',session.seconds.toFixed(2)+' 秒（含初始化与收尾）'],['可回放帧',String(data.frames.length)],
    ['有效帧',String(data.frames.filter(f=>f.valid).length)],
    ['安装高度',meta.installation?.height_m!=null?meta.installation.height_m+' 米':'未记录'],
    ['朝向',meta.installation?.orientation==='not_recorded'?'未记录':meta.installation?.orientation||'未记录'],
    ['当前修订',data.revision_id||'原始标签']];
  const list=$('session-facts');list.replaceChildren();
  for(const [key,value]of pairs){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;list.append(dt,dd);}
  $('session-path').textContent=session.path;
  $('session-notes').replaceChildren();
  const notes=[...(meta.note?[{text:meta.note}]:[]),...(meta.simulation_protocol?[{text:'本场声明的模拟方式：'+meta.simulation_protocol}]:[]),...data.notes];
  for(const row of notes){const li=document.createElement('li');li.textContent=(row.time_s!=null?row.time_s.toFixed(2)+' 秒：':'')+row.text;$('session-notes').append(li);}
  if(!notes.length){const li=document.createElement('li');li.textContent='没有现场备注';$('session-notes').append(li);}
  $('issue-details').hidden=!data.issues.length;$('session-issues').replaceChildren();
  for(const text of data.issues){const li=document.createElement('li');li.textContent=text;$('session-issues').append(li);}
}
async function openSession(){
  if(state.dirty||state.formDirty)throw new Error('有未保存修改。请先保存修订，或点击“放弃未保存修改”再打开其他会话。');
  const key=$('session-select').value;if(!key)return;
  stopPlay();++frameRequest;frameAbort?.abort();$('session-open').disabled=true;message('正在建立帧索引，大文件可能需要稍等……');
  try{
    const data=await api('/api/session?key='+encodeURIComponent(key));
    state.session=data;state.key=key;state.labels=clone(data.labels);state.baseline=clone(data.labels);
    state.baseRevision=data.revision_id;state.undo=[];state.download=data.revision_id?`/api/revision-download?key=${encodeURIComponent(key)}&revision=${encodeURIComponent(data.revision_id)}`:null;state.editing=null;state.formDirty=false;
    $('review-download').hidden=!state.download;$('review-confirmed').checked=false;$('review-reason').value='';
    $('quality-facts').replaceChildren();$('quality-issues').replaceChildren();$('features-result').textContent='';
    $('review-result').textContent='';$('label-undo').disabled=true;$('edit-form').reset();
    $('edit-status').textContent='选择左侧已有区间，或点击“新增区间”。';$('edit-label-picker').hidden=true;$('edit-action-name').textContent='请点击左侧动作区间';dirty(false);
    setFacts(data);renderLabels();$('replay-work').hidden=false;
    const frames=data.frames,slider=$('frame-slider');
    slider.min=String(frames.length?frames[0].time_s:0);slider.max=String(frames.length?frames[frames.length-1].time_s:0);slider.disabled=!frames.length;
    $('seek-seconds').max=String(data.session.seconds);$('seek-seconds').disabled=!frames.length;$('seek-go').disabled=!frames.length;
    const scale=$('time-scale');scale.replaceChildren();
    for(let i=0;i<=4;i++){const mark=document.createElement('span');mark.textContent=(Number(slider.min)+(Number(slider.max)-Number(slider.min))*i/4).toFixed(1)+' 秒';scale.append(mark);}
    $('replay-empty').hidden=!!data.frames.length;
    for(const id of ['play-toggle','frame-prev','frame-next'])$(id).disabled=!data.frames.length;
    $('fusion-result').textContent='';$('radar-replay-details').textContent='当前帧无雷达对照';
    if(data.frames.length)await showFrame(0);else{state.frame=null;drawHeat('replay-canvas',null);drawTimeline();$('frame-time').textContent='没有可回放帧';$('frame-quality').textContent='当前会话无有效索引帧';$('features-result').textContent='';}
    message(data.issues.length?'会话已打开，有读取问题，请展开查看。':'会话已打开；原始数据保持只读。',!!data.issues.length);
  }finally{$('session-open').disabled=false;}
}
async function showFrame(n){
  if(!state.session?.frames.length)return;
  n=Math.max(0,Math.min(state.session.frames.length-1,Number(n)));
  state.frameN=n;$('frame-slider').value=String(state.session.frames[n].time_s);drawTimeline();
  const request=++frameRequest,key=state.key;frameAbort?.abort();frameAbort=new AbortController();
  state.frame=null;$('frame-quality').textContent='正在读取所选帧……';
  $('radar-replay-details').textContent='正在读取本帧对应的雷达记录……';
  try{
    const row=await api(`/api/frame?key=${encodeURIComponent(key)}&n=${n}`,undefined,{signal:frameAbort.signal});
    if(request!==frameRequest||key!==state.key)return;
    state.frame=row;drawHeat('replay-canvas',row);
    $('features-result').textContent='';
    $('fusion-result').textContent='';
    $('frame-time').textContent=`${row.relative.time_s.toFixed(3)} 秒 · ${n+1}/${state.session.frames.length}`;
    $('frame-quality').textContent=qualityText(row);
    const radar=await api(`/api/radar?key=${encodeURIComponent(key)}&n=${n}`,undefined,{signal:frameAbort.signal});
    if(request===frameRequest&&key===state.key)$('radar-replay-details').textContent=radarText(radar)+(radar.issues?.length?' · 原始雷达记录有问题，需核查。':'');
  }catch(error){if(error.name!=='AbortError'&&request===frameRequest&&key===state.key){$('radar-replay-details').textContent='本帧雷达对照未读取成功，状态不可用。';stopPlay();message('帧或雷达读取失败：'+error.message,true);}}
}
function stopPlay(){state.playing=false;playEpoch=null;$('play-toggle').textContent='播放';}
function playTick(now){
  if(!state.playing)return;
  const frames=state.session.frames;
  if(!playEpoch)playEpoch={now,at:frames[state.frameN].time_s};
  const t=playEpoch.at+(now-playEpoch.now)/1000*Number($('play-speed').value);
  let next=state.frameN;
  while(next+1<frames.length&&frames[next+1].time_s<=t)next++;
  if(next!==state.frameN)showFrame(next);
  if(t-frames[next].time_s>3)$('frame-quality').textContent=`回放至 ${t.toFixed(1)} 秒：此处没有新帧，当前显示上一张记录；不能视为连续静止。`;
  if(next===frames.length-1){stopPlay();return;}
  requestAnimationFrame(playTick);
}
function drawTimeline(){
  if(!state.session)return;
  const canvas=$('timeline'),dpr=window.devicePixelRatio||1,w=canvas.getBoundingClientRect().width||1000,h=100;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d'),duration=state.session.session.seconds||1;
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);ctx.fillStyle='#e4e9e2';ctx.fillRect(0,12,w,18);ctx.fillRect(0,44,w,26);
  for(const frame of state.session.frames){
    ctx.fillStyle=frame.status==='error'?'#a12f30':frame.valid?'#36816b':'#a4afa7';
    const x=Number(frame.start_s)/duration*w,width=Math.max(1,(Number(frame.end_s)-Number(frame.start_s))/duration*w);
    ctx.fillRect(x,12,width,18);
  }
  for(const row of state.labels){
    ctx.fillStyle=row.label_id===state.editing?'#176852':'#8bb7a1';
    if(row.label==='unknown'||row.label==='invalid')ctx.fillStyle='#ba9960';
    const x=Number(row.start_s)/duration*w,width=(Number(row.end_s)-Number(row.start_s))/duration*w;
    ctx.fillRect(x,44,width,26);
  }
  const current=state.session.frames[state.frameN];
  if(current){const x=current.time_s/duration*w;ctx.fillStyle='#b76816';ctx.fillRect(x-1,5,2,70);}
  ctx.font='12px sans-serif';ctx.fillStyle='#526761';ctx.textBaseline='top';
  for(let i=0;i<=4;i++){const text=(duration*i/4).toFixed(1)+'s';ctx.textAlign=i===0?'left':i===4?'right':'center';ctx.fillText(text,w*i/4,82);}
}
function renderLabels(){
  const original=new Map(state.baseline.map(row=>[row.label_id,row]));
  const body=$('labels-body');body.replaceChildren();
  [...state.labels].sort((a,b)=>Number(a.start_s)-Number(b.start_s)).forEach(row=>{
    const before=original.get(row.label_id);const changed=!before||['label','start_s','end_s','note','boundary_uncertainty_ms'].some(key=>String(row[key]??'')!==String(before[key]??''));
    const tr=document.createElement('tr');tr.dataset.labelId=row.label_id;tr.classList.toggle('selected',row.label_id===state.editing);
    const td=document.createElement('td'),button=document.createElement('button');button.type='button';button.textContent=labelName(row.label)+(parsedContext(row)?' · '+parsedContext(row).body_angle_deg+'°':'');button.title=contextSummary(row);
    button.addEventListener('click',handle(()=>editLabel(row.label_id)));td.append(button);tr.append(td);
    for(const text of [Number(row.start_s).toFixed(3),Number(row.end_s).toFixed(3),row.end_reason==='recording_stopped_truncated'?'采集时截断':changed?'已修改未保存':'已载入']){
      const cell=document.createElement('td');cell.textContent=text;tr.append(cell);
    }
    const action=document.createElement('td'),remove=document.createElement('button');
    remove.type='button';remove.textContent='删除';remove.className='row-delete';remove.setAttribute('aria-label','删除'+labelName(row.label)+' '+Number(row.start_s).toFixed(2)+'至'+Number(row.end_s).toFixed(2)+'秒');
    remove.addEventListener('click',handle(()=>deleteLabel(row.label_id)));action.append(remove);tr.append(action);
    body.append(tr);
  });
  $('label-count').textContent=`(${state.labels.length})`;$('labels-empty').hidden=!!state.labels.length;drawTimeline();
}
function deleteLabel(id){
  if(!id)return;
  if(state.formDirty)throw new Error('请先应用或放弃当前未应用的编辑，再删除区间。');
  snapshot();state.labels=state.labels.filter(row=>row.label_id!==id);
  if(state.editing===id){state.editing=null;state.formDirty=false;$('edit-form').reset();$('edit-action-name').textContent='请点击左侧动作区间';$('edit-label-picker').hidden=true;}
  dirty();$('review-confirmed').checked=false;renderLabels();
  $('edit-status').textContent='已删除区间；可撤销，保存修订后生效。';message('已从待保存列表删除区间，可以撤销；原始标签未删除。');
}
function editLabel(id){
  if(state.formDirty)throw new Error('当前区间还有未应用的修改，请先“应用区间修改”，或放弃未保存修改。');
  const row=state.labels.find(item=>item.label_id===id);
  state.editing=row?.label_id||null;
  $('edit-heading').textContent=row?'编辑区间':'新增区间';
  $('edit-label-picker').hidden=!!row;
  $('edit-action-name').textContent=row?'当前动作：'+labelName(row.label)+(contextSummary(row)?'；采集条件：'+contextSummary(row):''):'新增区间：请选择动作';
  $('edit-label').value=row?.label||'stand_still';
  if(!$('edit-label').value)$('edit-label').value='unknown';
  const frame=state.session.frames[state.frameN];
  $('edit-start').value=row?.start_s??frame?.start_s??'0';
  $('edit-end').value=row?.end_s??frame?.end_s??'0';
  $('edit-note').value=row?.note||'';$('edit-uncertainty').value=row?.boundary_uncertainty_ms??'';
  $('label-delete').disabled=!row;state.formDirty=false;
  $('edit-status').textContent=row?'修改后点击“应用区间修改”。':'设置动作与边界后，应用到待保存列表。';renderLabels();
}
function applyEdit(event){
  event.preventDefault();if(!state.session)return;
  const start=$('edit-start').value.trim(),end=$('edit-end').value.trim();
  if(!/^\d+(\.\d{1,9})?$/.test(start)||!/^\d+(\.\d{1,9})?$/.test(end))throw new Error('时间需为非负十进制秒数，最多九位小数');
  if(Number(start)>=Number(end)||Number(end)>state.session.session.seconds)throw new Error('需满足：开始小于结束，且位于会话时长内');
  const old=state.labels.find(row=>row.label_id===state.editing),id=old?.label_id||crypto.randomUUID();
  const row={...(old||{}),label_id:id,label:$('edit-label').value,start_s:start,end_s:end,
    end_reason:old?.end_reason||'offline_annotation',note:$('edit-note').value,
    boundary_uncertainty_ms:$('edit-uncertainty').value,review_status:'pending'};
  snapshot();state.labels=state.labels.filter(item=>item.label_id!==id);state.labels.push(row);
  state.editing=id;state.formDirty=false;dirty();$('review-confirmed').checked=false;
  renderLabels();
  const summary=`已应用：${labelName(row.label)}，${Number(start).toFixed(3)}～${Number(end).toFixed(3)} 秒。`;
  $('edit-status').textContent=summary+'列表已更新，尚未保存修订。';
  const selected=$('labels-body').querySelector('tr.selected');const list=selected?.closest('.table-scroll');
  if(selected&&list){const r=selected.getBoundingClientRect(),p=list.getBoundingClientRect();if(r.top<p.top||r.bottom>p.bottom)list.scrollTop+=r.top-p.top-list.clientHeight/2+r.height/2;}
  message('区间已更新，请在下方保存标签修订。');
}
async function saveReview(){
  if(!state.session)return;
  if(state.formDirty)throw new Error('请先应用当前区间的修改，再保存修订。');
  const button=$('review-save');button.disabled=true;message('正在校验来源并保存独立修订……');
  try{
    const result=await api('/api/review?key='+encodeURIComponent(state.key),{
      labels:state.labels,annotator_id:$('reviewer').value,reason:$('review-reason').value,
      source_manifest_sha256:state.session.source_manifest_sha256,
      reviewed:$('review-confirmed').checked,base_revision_id:state.baseRevision});
    state.baseRevision=result.revision_id;state.labels=clone(result.labels);state.baseline=clone(result.labels);
    state.session.revision_id=result.revision_id;setFacts(state.session);
    state.download=result.download_url;state.undo=[];$('label-undo').disabled=true;dirty(false);
    $('review-download').hidden=false;$('review-result').textContent='已保存：'+result.path;
    renderLabels();message('标签修订已保存，原始记录未改动。');
  }finally{button.disabled=false;}
}
async function downloadReview(){
  if(!state.download)throw new Error('当前没有可下载的已提交修订。');
  const response=await fetch(state.download,{headers:{'X-Workbench-Token':localToken}});
  if(!response.ok)throw new Error('下载失败，请查看已保存的修订目录');
  const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');
  a.href=url;a.download=`${state.session.session.session_id}_labels.zip`;a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
async function checkQuality(){
  if(!state.key)return;
  const key=state.key,button=$('quality-run');button.disabled=true;
  try{
    const result=await api('/api/quality?key='+encodeURIComponent(key));if(key!==state.key)return;
    const facts=$('quality-facts');facts.replaceChildren();
    for(const [name,value] of [['数据结构',result.data_checks_ok?'未发现结构问题':'存在问题，需核对'],['索引 / 原始行',`${result.indexed_frames} / ${result.source_lines}`],['有效 / 非有效帧',`${result.valid_frames} / ${result.invalid_or_warmup_frames}`],['观察帧率',result.observed_fps==null?'无法计算':result.observed_fps.toFixed(2)+' 帧/秒'],['读取耗时中位数',result.read_ms.median==null?'无记录':result.read_ms.median.toFixed(1)+' ms'],['断档提示',`${result.gap_count} 处（帧间隔 > ${result.gap_threshold_seconds}s）`],['已保存标签',`${result.label_count} 段`]]){
      const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=value;facts.append(dt,dd);
    }
    $('quality-issues').replaceChildren();
    for(const text of [...result.frame_issues,...result.label_issues,...result.warnings]){const li=document.createElement('li');li.textContent=text;$('quality-issues').append(li);}
    if(state.dirty||state.formDirty)message('检查基于已保存标签；当前未保存的修改没有计入。');
  }finally{button.disabled=false;}
}
async function analyzeFrame(){
  if(!state.key||!state.frame)return;
  const key=state.key,n=state.frameN,button=$('features-run');button.disabled=true;
  try{
    const data=await api(`/api/features?key=${encodeURIComponent(key)}&n=${n}&delta_c=${encodeURIComponent($('feature-delta').value)}`);
    if(key!==state.key||n!==state.frameN)return;
    $('features-result').textContent=data.usable?`本帧中位温度 ${data.background_median_c.toFixed(2)}°C；候选温区 ${data.component_count} 个，最大 ${data.largest_pixels} 像素。`+(data.centroid_px?`重心位于原始矩阵第 ${(data.centroid_px.y+1).toFixed(1)} 行、第 ${(data.centroid_px.x+1).toFixed(1)} 列。`:'没有超过探索温差的温区。'):'预热、坏帧或读取异常，不计算温区特征。';
  }finally{button.disabled=false;}
}
async function analyzeFusion(){
  if(!state.key)return;
  const key=state.key,button=$('fusion-run');button.disabled=true;$('fusion-result').textContent='正在分析连续帧……';
  try{
    const data=await api('/api/fusion?key='+encodeURIComponent(key));
    if(key!==state.key)return;
    $('fusion-result').textContent=`已分析 ${data.processed_frames} 帧，发现 ${data.candidates.length} 个形状变化候选。`+(data.candidates.length?` 候选时刻：${data.candidates.map(x=>x.time_s.toFixed(2)+'秒').slice(0,20).join('、')}。`:'没有候选不代表已确认安全。')+' 参数尚未用标注数据校准，不计算准确率。';
  }catch(error){$('fusion-result').textContent='分析失败：'+error.message;throw error;}finally{button.disabled=false;}
}
const routeNames={needs_assistance:'协助请求',emergency_help:'紧急求助',fall_or_injury:'跌倒或受伤陈述',safety_response:'确认语境下的安全自述',uncertain:'待核查',normal:'一般表达'};
const sourceNames={kws_live:'KWS快速预警',kws:'KWS快速预警',llm:'本地语义理解',runtime_error:'处理异常'};
async function loadEvents(){
  const button=$('events-refresh');button.disabled=true;
  try{
    const result=await api('/api/events-preview');state.events=result.events;
    $('events-issues').replaceChildren();for(const text of result.issues){const li=document.createElement('li');li.textContent=text;$('events-issues').append(li);}
    $('events-empty').hidden=!!state.events.length;$('events-work').hidden=!state.events.length;
    if(!state.events.some(row=>row.event_id===state.eventId))state.eventId=state.events[0]?.event_id||null;
    renderEvents();
  }finally{button.disabled=false;}
}
function renderEvents(){
  $('events-list').replaceChildren();
  for(const row of state.events){
    const button=document.createElement('button');button.type='button';button.className='event-row';
    button.setAttribute('aria-pressed',String(row.event_id===state.eventId));
    const title=document.createElement('strong'),details=document.createElement('span');
    title.textContent=(row.red_alert_latched===true?'红色锁存 · ':row.red_alert_latched===false?'':'状态未记录 · ')+(routeNames[row.route]||row.route);
    details.textContent=`${row.source_session} · 第${row.ordinal}条 · ${sourceNames[row.source]||row.source}`;
    button.append(title,details);button.addEventListener('click',()=>{state.eventId=row.event_id;$('event-human-checked').checked=false;$('event-note').value='';$('event-feedback').textContent='';renderEvents();});
    $('events-list').append(button);
  }
  const row=state.events.find(item=>item.event_id===state.eventId);if(!row)return;
  const fields=[['来源','历史语音记录；非实时'],['事件类别',routeNames[row.route]||row.route],['提取事实',row.events.join('、')||'无明确事实'],['处理状态',row.model_status==='ok'?'成功':row.model_status==='unknown'?'未记录':row.model_status],['历史红色锁存',row.red_alert_latched===true?'保持':row.red_alert_latched===false?'未锁存':'未记录'],['通知送达','未核验，无送达凭证'],['演练进度',row.rehearsal.closed?'已结束（历史告警不变）':row.rehearsal.acknowledged?'已确认，待模拟人工核查':'未确认'],['记录时间',row.mono_ns?'原设备单调时钟 '+row.mono_ns+' ns':'未记录']];
  $('event-facts').replaceChildren();for(const [name,value]of fields){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=value;$('event-facts').append(dt,dd);}
  $('event-reason').textContent=row.reason;$('event-transcript').textContent=row.transcript?`原转写：${row.transcript}`:'此快照未保存逐句转写；不补造语音内容。';
  $('event-ack').disabled=row.rehearsal.acknowledged;$('event-close').disabled=!row.rehearsal.acknowledged||row.rehearsal.closed;
}
async function rehearseEvent(action){
  if(!state.eventId)return;
  const key=state.eventId;$('event-ack').disabled=true;$('event-close').disabled=true;
  try{
    await api('/api/events-rehearse',{event_id:key,action,operator:$('event-operator').value,reason:$('event-note').value,human_checked:$('event-human-checked').checked});
    await loadEvents();$('event-feedback').textContent=action==='close'?'演练流程已结束。历史告警记录保持原样，没有操作板子或发送通知。':'演练确认完成。确认不等于告警解除，也不代表通知已送达。';
  }finally{renderEvents();}
}
async function init(){
  state.config=await api('/api/config');
  $('radar-enabled').disabled=!state.config.radar_configurable;
  $('radar-config-note').textContent=state.config.radar_configurable?`雷达可选；当前主机接收 ${state.config.radar_host_baud} 8N1，配置由启动参数提供。打开网页不会访问雷达。`:'此实例仅回放，不访问串口；可查看已保存的真实雷达记录。';
  state.labelsById=new Map(state.config.label_schema.labels.map(item=>[item.id,item]));
  initTargetedFields();
  for(const p of state.config.label_schema.recording_profiles){const option=document.createElement('option');option.value=p.id;option.textContent=p.name;$('recording-profile').append(option);}
  $('recording-profile').value=state.config.label_schema.default_recording_profile;
  fillLabels($('edit-label'));
  syncCollectionFields();
  const prefill=new URL(location.href).searchParams;
  for(const key of ['scene','participant','annotator_id','duration_minutes']){
    if(prefill.has(key))$('capture-form').elements.namedItem(key).value=prefill.get(key);
  }
  $('capture-availability').textContent=state.config.capture_enabled?'先固定传感器并确认只有一个热像读取程序，再开始本次采集。':'此 PC 实例仅供离线回放；现场采集需要在 K1 启用。无需让开发板持续在线。';
  $('tab-capture').addEventListener('click',()=>setMode('capture'));
  $('tab-replay').addEventListener('click',handle(async()=>{setMode('replay');if(!state.dirty&&!state.formDirty)await scanSessions();}));
  $('tab-events').addEventListener('click',handle(async()=>{setMode('events');await loadEvents();}));
  $('events-refresh').addEventListener('click',handle(loadEvents));
  $('event-ack').addEventListener('click',handle(()=>rehearseEvent('acknowledge')));
  $('event-close').addEventListener('click',handle(()=>rehearseEvent('close')));
  $('live-label').addEventListener('change',event=>selectLiveLabel(event.target.value));
  $('recording-profile').addEventListener('change',prepareTargetedMode);
  $('targeted-action').addEventListener('change',prepareTargetedMode);
  $('targeted-count').addEventListener('input',syncTargetedPanel);
  $('targeted-angles').addEventListener('change',syncTargetedPanel);
  for(const [id,delta]of [['targeted-count-minus',-1],['targeted-count-plus',1]])$(id).addEventListener('click',()=>{$('targeted-count').value=String(Math.max(1,Math.min(100,(Number($('targeted-count').value)||1)+delta)));syncTargetedPanel();});
  $('targeted-next-angle').addEventListener('click',()=>{const select=$('targeted-live-angle');if(select.disabled||!select.options.length)return;select.selectedIndex=(select.selectedIndex+1)%select.options.length;message('已选择'+select.selectedOptions[0].textContent+'；请按实际调整人体朝向，准备好后再开始动作。');});
  $('targeted-reset').addEventListener('click',handle(()=>captureCommand('targeted_reset')));
  $('targeted-protection-preset').addEventListener('click',()=>{$('simulation-protocol').value='采集者声明：本次使用软垫并由现场协助者保护，进行受控下降；不要求失控摔倒或追求速度。';message('已按你的点击填入保护说明，请确认与本次实际条件一致。');});
  $('collection-purpose').addEventListener('change',()=>{
    if($('collection-purpose').value==='device_check')$('activity-source').value='equipment_only';
    else if($('activity-source').value==='equipment_only')$('activity-source').value='everyday_only';
    syncCollectionFields();
  });
  $('activity-source').addEventListener('change',()=>{
    if($('activity-source').value==='equipment_only')$('collection-purpose').value='device_check';
    else if($('collection-purpose').value==='device_check')$('collection-purpose').value='functional_trial';
    syncCollectionFields();
  });
  $('capture-form').addEventListener('invalid',()=>{$('capture-setup').open=true;},true);
  $('capture-form').addEventListener('submit',handle(async event=>{
    event.preventDefault();if(state.captureBusy)return;state.captureBusy=true;$('capture-start').disabled=true;
    try{
      const data=Object.fromEntries(new FormData(event.target));
      if(targetedMode()){
        data.targeted_collection={target_id:$('targeted-action').value,angles:targetedAngles(),repetitions_per_angle:Number($('targeted-count').value)};
        if(!data.targeted_collection.angles.length){$('capture-setup').open=true;throw new Error('请至少选择一个专项采集角度');}
      }
      data.radar_enabled=$('radar-enabled').checked;
      if(data.activity_source!=='everyday_and_controlled')data.simulation_protocol='';
      await api('/api/capture/start',data);$('capture-setup').open=false;message('正在初始化热像设备，初始化完成后可标记动作。');
    }
    finally{state.captureBusy=false;await pollCapture();}
  }));
  $('label-start').addEventListener('click',handle(()=>captureCommand('label_start',{label:$('live-label').value})));
  $('label-end').addEventListener('click',handle(()=>captureCommand('label_end',{label_id:state.capture?.active?.label_id})));
  $('capture-stop').addEventListener('click',handle(()=>captureCommand('stop')));
  $('capture-cancel').addEventListener('click',handle(async()=>{
    if(!window.confirm('取消本次采集？已录内容将移入回收区，不再出现在数据集列表，之后可以恢复。'))return;
    await captureCommand('cancel',{confirm_discard:true});
  }));
  $('preview-start').addEventListener('click',handle(()=>previewCommand('start')));
  $('preview-stop').addEventListener('click',handle(()=>previewCommand('stop')));
  $('fall-test-toggle').addEventListener('click',handle(()=>previewCommand(previewRunning()?'stop':'fall-test')));
  $('note-add').addEventListener('click',handle(async()=>{await captureCommand('note',{text:$('capture-note').value});$('capture-note').value='';}));
  $('session-refresh').addEventListener('click',handle(async()=>{if(state.dirty||state.formDirty)throw new Error('请先保存或放弃未保存修改，再刷新列表。');await scanSessions();message('会话列表已刷新。');}));
  $('session-date').addEventListener('change',renderSessionOptions);
  $('session-select').addEventListener('change',syncRecovery);
  $('trash-refresh').addEventListener('click',handle(loadTrash));
  $('trash-panel').addEventListener('toggle',handle(async()=>{if($('trash-panel').open)await loadTrash();}));
  $('dataset-rename').addEventListener('click',handle(async()=>{
    const key=$('session-select').value;if(!key)return;
    if(state.dirty||state.formDirty)throw new Error('请先保存或放弃当前标签修改，再修改名称。');
    const result=await api('/api/session/name?key='+encodeURIComponent(key),{display_name:$('dataset-display-name').value});
    await scanSessions();
    if(state.key===key&&state.session){state.session.session=result;setFacts(state.session);}
    message('显示名称已保存，内部会话ID及原始数据未改变。');
  }));
  $('session-delete').addEventListener('click',handle(async()=>{
    if(state.dirty||state.formDirty)throw new Error('请先保存或放弃当前标签修改，再删除数据集。');
    const key=$('session-select').value,row=state.library.find(item=>item.key===key);if(!row)return;
    if(!window.confirm(`将“${row.display_name||row.scene||row.session_id}”移入回收区？原始数据和标签修订将一起保留，可恢复。\n内部ID：${row.session_id}`))return;
    $('session-delete').disabled=true;stopPlay();++frameRequest;frameAbort?.abort();
    await api('/api/session/trash?key='+encodeURIComponent(key),{confirmed_session_id:row.session_id});
    if(state.key===key){state.key=null;state.session=null;state.labels=[];state.baseline=[];state.frame=null;state.undo=[];$('replay-work').hidden=true;}
    await scanSessions();await loadTrash();message('数据集已移入回收区，可展开回收区恢复。');
  }));
  $('recovery-open').addEventListener('click',handle(async()=>{
    if(state.dirty||state.formDirty)throw new Error('请先保存或放弃当前未保存修改。');
    if(!$('recovery-confirmed').checked)throw new Error('请先确认原采集已停止，目录不再写入。');
    $('recovery-open').disabled=true;
    try{
      const key=$('session-select').value;
      await api('/api/recover?key='+encodeURIComponent(key),{confirmed_stopped:true,annotator_id:$('recovery-reviewer').value,reason:$('recovery-reason').value});
      await scanSessions();$('session-select').value=key;syncRecovery();await openSession();
      message('已打开只读中断视图。未闭合动作需根据记录核对，不能当正常完成。');
    }finally{$('recovery-open').disabled=false;}
  }));
  $('session-open').addEventListener('click',handle(openSession));
  $('frame-prev').addEventListener('click',()=>{stopPlay();showFrame(state.frameN-1);});
  $('frame-next').addEventListener('click',()=>{stopPlay();showFrame(state.frameN+1);});
  function seekTime(t){
    const frames=state.session?.frames;if(!frames?.length||!Number.isFinite(t))return;
    let lo=0,hi=frames.length-1;
    while(lo<hi){const mid=Math.floor((lo+hi)/2);if(Number(frames[mid].time_s)<t)lo=mid+1;else hi=mid;}
    const n=lo>0&&Math.abs(Number(frames[lo-1].time_s)-t)<=Math.abs(Number(frames[lo].time_s)-t)?lo-1:lo;
    stopPlay();showFrame(n);
  }
  $('frame-slider').addEventListener('input',event=>seekTime(Number(event.target.value)));
  $('seek-go').addEventListener('click',()=>{const input=$('seek-seconds');if(!input.value||!input.reportValidity())return;seekTime(Number(input.value));});
  $('seek-seconds').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();$('seek-go').click();}});
  $('play-toggle').addEventListener('click',()=>{if(state.playing){stopPlay();return;}if(state.frameN>=state.session.frames.length-1)showFrame(0);state.playing=true;playEpoch=null;$('play-toggle').textContent='暂停';requestAnimationFrame(playTick);});
  $('play-speed').addEventListener('change',()=>{playEpoch=null;});
  $('timeline').addEventListener('click',event=>{
    const frames=state.session?.frames;if(!frames?.length)return;stopPlay();
    const r=event.currentTarget.getBoundingClientRect(),t=Math.max(0,Math.min(1,(event.clientX-r.left)/r.width))*state.session.session.seconds;
    let nearest=0;for(let i=1;i<frames.length;i++)if(Math.abs(frames[i].time_s-t)<Math.abs(frames[nearest].time_s-t))nearest=i;
    showFrame(nearest);
  });
  $('label-new').addEventListener('click',handle(()=>editLabel(null)));
  $('edit-form').addEventListener('submit',event=>{try{applyEdit(event);}catch(error){$('edit-status').textContent='未应用：'+error.message;message(error.message,true);}});
  $('edit-form').addEventListener('invalid',event=>{$('edit-status').textContent='未应用：请检查必填项及输入格式。';},true);
  for(const id of ['edit-label','edit-start','edit-end','edit-uncertainty','edit-note'])$(id).addEventListener('input',()=>{state.formDirty=true;$('edit-status').textContent='区间编辑尚未应用。';});
  for(const [id,field,edge] of [['set-start','edit-start','start_s'],['set-end','edit-end','end_s']])$(id).addEventListener('click',()=>{const frame=state.session?.frames[state.frameN];if(frame){$(field).value=frame[edge];state.formDirty=true;$('edit-status').textContent='边界取自本帧读取区间，仍需人工核对。';}});
  $('label-delete').addEventListener('click',handle(()=>deleteLabel(state.editing)));
  $('label-undo').addEventListener('click',()=>{if(!state.undo.length)return;state.labels=state.undo.pop();state.editing=null;state.formDirty=false;$('edit-form').reset();$('review-confirmed').checked=false;$('label-undo').disabled=!state.undo.length;dirty();renderLabels();});
  $('discard-changes').addEventListener('click',()=>{state.labels=clone(state.baseline);state.undo=[];state.editing=null;state.formDirty=false;$('edit-form').reset();$('label-undo').disabled=true;dirty(false);renderLabels();message('已恢复到最近载入或保存的版本。');});
  $('review-save').addEventListener('click',handle(saveReview));$('review-download').addEventListener('click',handle(downloadReview));
  $('quality-run').addEventListener('click',handle(checkQuality));$('features-run').addEventListener('click',handle(analyzeFrame));$('fusion-run').addEventListener('click',handle(analyzeFusion));
  for(const id of ['temp-min','temp-max'])$(id).addEventListener('change',handle(()=>{scale();drawHeat('live-canvas',state.capture?.latest);drawHeat('replay-canvas',state.frame);}));
  $('live-canvas').addEventListener('pointermove',event=>pixelInfo(event,event.currentTarget,state.capture?.latest,'live-pixel'));
  $('replay-canvas').addEventListener('pointermove',event=>pixelInfo(event,event.currentTarget,state.frame,'replay-pixel'));
  window.addEventListener('beforeunload',event=>{if(state.dirty||state.formDirty){event.preventDefault();event.returnValue='';}});
  window.addEventListener('resize',()=>{if(state.session)drawTimeline();});
  setMode(state.config.capture_enabled?'capture':'replay');await pollCapture();await scanSessions();setInterval(pollCapture,1000);
}
init().catch(error=>message('工作台初始化失败：'+error.message+'。请确认本机服务仍在运行，再刷新页面。',true));
