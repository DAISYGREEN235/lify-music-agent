'use strict';
const $ = id => document.getElementById(id);
const audio = $('audio');
let session = localStorage.getItem('lify.task') || 'default';
let token = '', result = null, savedPlayer = null, index = -1, busy = false, tracking = false, restoreAt = 0;
let queue = Promise.resolve();
let watchedJob = null, historyCursor = null;
const client = crypto.randomUUID();
function note(text) { $('status').textContent = text; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json','X-Lify-Token':token}, body:JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) { const error=new Error(typeof data.detail === 'string' ? data.detail : '输入不符合要求，请检查后重试');error.session=data.session;throw error; }
  return data;
}
function el(tag, text, className='') { const node=document.createElement(tag); node.textContent=text; node.className=className; return node; }
function telemetry(event, active=!audio.paused && !audio.seeking) {
  if (!tracking || index < 0 || !result?.items[index]) return Promise.resolve();
  const body={session,client,track_id:result.items[index].track.id,event,
    position:Number.isFinite(audio.currentTime)?audio.currentTime:0,active,speed:audio.playbackRate};
  queue=queue.catch(()=>{}).then(()=>api('/api/playback',body));
  queue.catch(()=>note('播放记录未同步，请检查本地服务；声音可能仍由浏览器继续播放。'));
  return queue;
}
async function selectTrack(i, play=true, start=0, finish='select') {
  if (!result?.items[i]) return;
  if (tracking) { await telemetry(finish,false); tracking=false; }
  audio.pause(); index=i; restoreAt=start;
  const t=result.items[i].track;
  $('now-title').textContent=t.title; $('now-artist').textContent=t.artist;
  audio.src=`/audio/${encodeURIComponent(t.id)}?session=${encodeURIComponent(session)}&key=${encodeURIComponent(token)}`;
  audio.load();
  $('previous').disabled=i===0; $('next').disabled=i>=result.items.length-1;
  if (play) { try { await audio.play(); } catch(error) { note('请点击下方播放器的播放按钮：'+error.message); } }
}
audio.addEventListener('loadedmetadata',()=>{ if(restoreAt>0) {audio.currentTime=Math.min(restoreAt,Math.max(0,audio.duration-.1));restoreAt=0;} });
audio.addEventListener('playing',()=>{ if(!tracking){tracking=true;telemetry('start',true);}else telemetry('sample',true); });
audio.addEventListener('pause',()=>telemetry('pause',false));
audio.addEventListener('seeking',()=>telemetry('seek',false));
audio.addEventListener('seeked',()=>telemetry('sample',!audio.paused));
audio.addEventListener('ratechange',()=>telemetry('seek',false));
audio.addEventListener('ended',async()=>{await telemetry('ended',false);tracking=false;if(index+1<result.items.length)await selectTrack(index+1,true);});
audio.addEventListener('error',()=>{if(!audio.getAttribute('src'))return;telemetry('error',false);tracking=false;note('该文件未能在浏览器中播放，请尝试其他歌曲。');});
setInterval(()=>{if(tracking&&!audio.paused)telemetry('sample');},750);
window.addEventListener('pagehide',()=>{
  if(tracking&&result?.items[index])fetch('/api/playback',{method:'POST',keepalive:true,
    headers:{'Content-Type':'application/json','X-Lify-Token':token},
    body:JSON.stringify({session,client,track_id:result.items[index].track.id,event:'close',
      position:audio.currentTime,active:false,speed:audio.playbackRate})}).catch(()=>{});
});
$('next').onclick=()=>selectTrack(index+1,true,0,'next').catch(e=>note(e.message));
$('previous').onclick=()=>selectTrack(index-1,true).catch(e=>note(e.message));
function render() {
  const items=result?.items||[];
  $('summary').textContent=result?`${items.length} 首 · 总时长 ${(result.total_seconds/60).toFixed(1)} 分钟`+
    (result.duration_deviation!==null&&result.duration_deviation!==undefined?` · 时长偏差 ${(result.duration_deviation*100).toFixed(1)}%`:''):'尚未载入歌单';
  $('warnings').replaceChildren(...(result?.warnings||[]).map(x=>el('p',x,'notice')));
  $('adjust').disabled=!items.length||busy;
  const intent=result?.intent;
  $('constraints').replaceChildren();
  if(intent){const values=[['场景',intent.scene],['希望听到',intent.target_mood],['语言',intent.language],
    ['数量',`${intent.count_mode==='exact'?'恰好':'约'} ${intent.count} 首`],
    ['时长',intent.minutes?`${intent.duration_mode==='max'?'不超过':'大约'} ${intent.minutes} 分钟`:'不限'],
    ['听感',intent.audio_query],['歌词主题',intent.lyrics_query],['排除歌曲',intent.exclude_tracks?.join('、')]];
    for(const [name,value] of values)if(value)$('constraints').append(el('p',`${name}：${value}`));}
  const list=$('playlist');list.replaceChildren();
  if(!items.length){list.append(el('p',result?'当前条件下没有可播放的歌单。请查看条件及不足说明，再修改需求。':'先描述场景，或载入已有歌单。','empty'));return;}
  items.forEach((item,i)=>{
    const t=item.track, row=el('article','','track'), top=el('div','','track-head'), title=el('div','');
    title.append(el('h3',`${i+1}. ${t.title}`),el('span',`${t.artist} · ${(t.duration/60).toFixed(1)} 分钟`,'muted'));
    const play=el('button','试听');play.onclick=()=>selectTrack(i).catch(e=>note(e.message));
    const ban=el('button','不再推荐');ban.onclick=async()=>{try{await api('/api/exclusion',{track_id:t.id,active:true});note('已设为此用户永久排除，可在画像区域撤销。');await refresh(false);}catch(e){note(e.message);}};
    top.append(title,play,ban);row.append(top);
    const details=el('details','','reasons');details.append(el('summary','选曲理由与来源'));
    for(const [route,evidence] of Object.entries(item.evidence||{}))details.append(el('p',
      `${({audio:'听感',text:'歌词主题',catalog:'曲库条件',topic_audit:'题材审核'})[route]||route}：${evidence.query}；`+
      (['audio','text'].includes(route)?`相对相关度 ${Number(evidence.similarity).toFixed(3)}；`:'')+
      `来源 ${evidence.source||'未知'}；版本 ${evidence.version||evidence.model||'未知'}`+
      (evidence.quote?`；引用：${evidence.quote}`:'')+(evidence.limitation?`；边界：${evidence.limitation}`:''),'muted'));
    for(const adjustment of item.ranking_adjustments||[])details.append(el('p',
      `${adjustment.source==='recent_3_playlists'?'最近三份歌单重复降权':'你的同场景反馈'}：排序系数 ${adjustment.factor}`,'muted'));
    const card=item.knowledge_card;
    if(card)details.append(el('p',`知识卡 ${card.document_id} · ${card.claims.length} 条可追溯依据`,'muted'));
    const tags=t.audio_tag_profile;
    if(tags){
      const dimensions={mood:'情绪',vocal:'人声',genre:'曲风',instrument:'乐器',texture:'音色'};
      details.append(el('p','音频标签估计（相对相似度）：'+tags.product_tags.map(tag=>
        `${dimensions[tag.namespace]||tag.namespace}：${tag.label} (${tag.score.toFixed(3)})`).join('；'),'muted'));
      details.append(el('p',`${tags.limitation}；来源 ${tags.source}；版本 ${tags.version}`,'muted'));
    }
    const policyButton=el('button','查看选曲与证据规则'), policyText=el('pre','','muted');
    policyButton.onclick=async()=>{policyButton.disabled=true;try{
      const value=await api('/api/policies');
      policyText.textContent=value.documents.map(doc=>`${doc.content}\n来源：${doc.source}`).join('\n\n');
      if(value.missing_documents.length)policyText.textContent+='\n部分策略文档不可用。';
    }catch(e){policyText.textContent=e.message;}finally{policyButton.disabled=false;}};
    details.append(policyButton,policyText);
    row.append(details);
    const form=el('div','','feedback'), rating=document.createElement('select'), dimension=document.createElement('select');
    rating.setAttribute('aria-label',`${t.title} 适配评分`);dimension.setAttribute('aria-label',`${t.title} 评价维度`);
    [[2,'适合'],[1,'部分适合'],[0,'不适合']].forEach(([v,label])=>rating.add(new Option(label,String(v))));
    [['overall','整体'],['topic','题材'],['sound','听感']].forEach(([v,label])=>dimension.add(new Option(label,v)));
    const reason=document.createElement('textarea');reason.placeholder='简短原因，例如：题材不适合这次休息';reason.setAttribute('aria-label',`${t.title} 反馈原因`);
    const save=el('button','保存反馈');save.onclick=async()=>{save.disabled=true;try{
      const value=await api('/api/feedback',{session,track_id:t.id,rating:Number(rating.value),dimension:dimension.value,reason:reason.value});
      note('反馈已保存。'+(value.effective.conflict?'文字与评分冲突，以明确文字为准。':value.effective.rating===null?'文字含义待解释，未改变偏好。':'')+'需要时点击“按反馈调整歌单”。');await refresh(false);
    }catch(e){note(e.message);}finally{save.disabled=false;}};
    form.append(rating,dimension,reason,save);row.append(form);
    const labels=el('details');labels.append(el('summary',`演唱语言：${t.language||'未知'} · 试听纠正`));
    const language=document.createElement('select');
    [['unknown','未知'],['zh','中文'],['en','英文'],['ja','日文'],['ko','韩文'],['mixed','明显双语'],['instrumental','纯器乐']].forEach(([v,label])=>language.add(new Option(label,v)));
    language.value=['unknown','zh','en','ja','ko','mixed','instrumental'].includes(t.language)?t.language:'unknown';
    const source=document.createElement('input');source.placeholder='来源：例如，已试听整首，主要英语演唱';
    const labelSave=el('button','确认语言'),labelUndo=el('button','撤销纠正');
    labelSave.onclick=async()=>{try{await api('/api/label',{track_id:t.id,language:language.value,source:source.value});note('演唱语言已记录，下次生成生效。');}catch(e){note(e.message);}};
    labelUndo.onclick=async()=>{try{await api('/api/label',{track_id:t.id,revoke:true});note('人工纠正已撤销，历史保留。');}catch(e){note(e.message);}};
    labels.append(language,source,labelSave,labelUndo);row.append(labels);list.append(row);
  });
}
async function refresh(draw=true) {
  const data=await api('/api/state?session='+encodeURIComponent(session));token=data.token;savedPlayer=data.player;
  if(draw)result=data.result;
  if(draw){$('clarify').hidden=!data.question;if(data.question)$('question').textContent=data.question;}
  const context=data.pending_context;
  $('ambiguity').hidden=!context;
  if(context){$('ambiguity').textContent=`已进行 ${context.clarification_rounds} / 2 轮澄清。剩余歧义：${context.ambiguity}。`+(context.waiting_for_input?'请主动补充并点击“回答”或修改需求；不会继续自动追问。':'');
    if(draw&&context.intent)result={...(result||{items:[],warnings:[],total_seconds:0}),intent:context.intent};
    if(context.waiting_for_input)$('clarify').hidden=false;
    if(context.waiting_for_input)$('question').textContent='等待你主动补充或修改条件';}
  $('tasks').replaceChildren(...data.tasks.map(t=>new Option(`${t.scene} · ${t.count}首 (${t.id.slice(0,18)})`,t.id)));
  if(data.tasks.some(t=>t.id===session))$('tasks').value=session;
  $('history').textContent=data.events.map(e=>e.kind==='web_feedback'?`评价 ${e.track_id}：${['不适合','部分适合','适合'][e.payload.rating]} · ${e.payload.dimension}\n${e.payload.reason}`:`${e.payload.role==='user'?'你':'Lify'}：${e.payload.text}`).join('\n\n')||'暂无交互或反馈记录';
  $('feedback-events').replaceChildren(...data.events.filter(e=>e.kind==='web_feedback'&&!e.payload.revokes).map(e=>new Option(`${e.track_id} · ${e.payload.reason||'评分反馈'}`,e.id)));
  $('exclusions').replaceChildren(...data.exclusions.map(id=>new Option(id,id)));
  $('budget').textContent=`预算记账 ¥${data.budget.accounted.toFixed(3)} / ¥${data.budget.limit}，预留 ¥${data.budget.held.toFixed(3)}`;
  $('restore-player').disabled=!savedPlayer||!result?.items?.length;
  if(draw){$('life-stage').value=data.profile.life_stage||'';$('common-scenes').value=(data.profile.common_scenes||[]).join('\n');$('stable-preferences').value=(data.profile.stable_preferences||[]).join('\n');}
  $('language-coverage').textContent='演唱语言分类覆盖：'+Object.entries(data.language_coverage).map(([k,v])=>`${k}: ${v}`).join('，');
  const page=await api('/api/history?session='+encodeURIComponent(session));historyCursor=page.next_cursor;
  $('older-history').disabled=!historyCursor;
  if(draw)render();
  if(data.job&&['queued','running'].includes(data.job.status)&&watchedJob!==data.job.id)watchJob(data.job.id,data.job.session);
}
function controls(disabled) {
  busy=disabled;
  document.querySelectorAll('#generate,#modify,#send-answer,#resume-task,#adjust').forEach(b=>b.disabled=disabled);
  if(!disabled)$('adjust').disabled=!result?.items?.length;
}
async function watchJob(jobId, taskSession) {
  watchedJob=jobId;controls(true);
  const labels={starting:'排队等待',parse:'理解需求',clarify:'整理澄清条件',choose:'选择检索工具',retrieve:'检索歌曲',compose:'组合并校验歌单'};
  try {
    while(watchedJob===jobId&&session===taskSession) {
      const job=await api('/api/progress/'+encodeURIComponent(jobId));
      if(['completed','failed','interrupted'].includes(job.status)) {
        if(job.status!=='completed') {note(job.error+'；可点击“恢复任务”。');await refresh(false);break;}
        const value=job.result;
        if(!value.question&&!value.waiting_for_input){
          audio.pause();audio.removeAttribute('src');audio.load();index=-1;result=value;
          $('now-title').textContent='尚未播放';
        }
        await refresh();
        note(value.question?'请补充关键条件。':value.waiting_for_input?'澄清已到两轮，请主动修改条件。':
          value.items?.length?'歌单已生成，点击试听。':'没有足够候选，请查看条件与缺口。');
        break;
      }
      note(`任务已受理 · ${labels[job.stage]||job.stage} · ${job.elapsed_seconds} 秒。关闭页面仍继续。`);
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
  } catch(e){note('无法读取后台状态：'+e.message+'。刷新查看，切勿重复提交。');}
  finally{if(watchedJob===jobId){watchedJob=null;controls(false);}}
}
async function send(mode,text) {
  if(busy)return;
  if(tracking&&!audio.paused){note('请先暂停试听，再创建或调整歌单。');return;}
  if(tracking){await telemetry('select',false);tracking=false;}
  controls(true);note('正在提交任务……');
  try {
    const job=await api('/api/message',{session,text,mode,request_id:crypto.randomUUID()});
    session=job.session;localStorage.setItem('lify.task',session);
    note('任务已受理，后台继续生成，不会自动播放。');
    await watchJob(job.id,session);
  }catch(e){note(e.message);controls(false);}
}
$('generate').onclick=()=>send('new',$('request').value);
$('modify').onclick=()=>send('modify',$('request').value);
$('send-answer').onclick=()=>send('answer',$('answer').value);
$('resume-task').onclick=()=>send('resume','');
$('adjust').onclick=()=>send('adjust','按本任务已经保存的反馈调整歌单，其他条件保留。');
$('load').onclick=async()=>{if(tracking){await telemetry('select',false);tracking=false;}audio.pause();audio.removeAttribute('src');audio.load();index=-1;
  session=$('tasks').value||'default';localStorage.setItem('lify.task',session);try{await refresh();note('歌单已载入，尚未播放。');}catch(e){note(e.message);}};
$('restore-player').onclick=()=>{const i=result?.items.findIndex(x=>x.track.id===savedPlayer?.track_id);if(i>=0)selectTrack(i,false,savedPlayer.position).then(()=>note('已恢复位置，点击下方播放按钮继续。')).catch(e=>note(e.message));};
$('save-profile').onclick=async()=>{try{await api('/api/profile',{life_stage:$('life-stage').value,
  common_scenes:$('common-scenes').value.split('\n').filter(x=>x.trim()),stable_preferences:$('stable-preferences').value.split('\n').filter(x=>x.trim())});note('显式画像已保存，新请求时作为默认参考。');}catch(e){note(e.message);}};
$('delete-profile').onclick=async()=>{try{await api('/api/profile',{delete:true});await refresh(false);$('life-stage').value='';$('common-scenes').value='';$('stable-preferences').value='';note('画像已删除，历史事件保留。');}catch(e){note(e.message);}};
$('undo-exclusion').onclick=async()=>{try{await api('/api/exclusion',{track_id:$('exclusions').value,active:false});await refresh(false);note('已撤销永久排除。');}catch(e){note(e.message);}};
$('older-history').onclick=async()=>{try{const data=await api('/api/history?session='+encodeURIComponent(session)+'&before='+historyCursor);historyCursor=data.next_cursor;
  $('history').textContent=data.events.map(e=>`${e.payload.role||e.kind}：${e.payload.text||e.payload.reason||''}`).join('\n\n')+'\n\n'+$('history').textContent;
  $('older-history').disabled=!historyCursor;}catch(e){note(e.message);}};
$('export-task').onclick=()=>{window.location.href='/api/export?session='+encodeURIComponent(session);};
$('report-issue').onclick=async()=>{try{const data=await api('/api/report',{session,text:$('issue-text').value});note('问题报告已保存：'+data.path);}catch(e){note(e.message);}};
$('revoke-feedback').onclick=async()=>{try{await api('/api/feedback/revoke',{session,event_id:$('feedback-events').value});await refresh(false);note('反馈已撤销并保留历史，需要时再点击按反馈调整。');}catch(e){note(e.message);}};
$('shutdown').onclick=async()=>{try{audio.pause();if(tracking){await telemetry('close',false);tracking=false;}await api('/api/shutdown',{});note('本地服务已停止。下次双击启动入口可以恢复任务和播放位置。');}catch(e){note(e.message);}};
refresh().catch(e=>note('无法载入本地服务：'+e.message));
