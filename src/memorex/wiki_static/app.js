const search=document.querySelector('#search');
if(search)search.addEventListener('input',()=>{
  const q=search.value.toLowerCase();
  document.querySelectorAll('#pages .card').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(q));
});

const previewButton=document.querySelector('#preview-note');
if(previewButton){
  previewButton.addEventListener('click',async()=>{
    const preview=document.querySelector('#note-preview');
    const body=document.querySelector('#note-body');
    const form=new FormData();form.set('body',body.value);
    previewButton.disabled=true;
    try{
      const response=await fetch('/markdown/preview',{method:'POST',body:form});
      if(!response.ok)throw new Error('preview failed');
      preview.innerHTML=await response.text();preview.hidden=false;
    }catch(_error){preview.textContent='Не удалось построить предпросмотр.';preview.hidden=false;}
    finally{previewButton.disabled=false;}
  });
}

document.querySelectorAll('.drop').forEach(drop=>{
  drop.addEventListener('dragover',event=>{event.preventDefault();drop.classList.add('active')});
  drop.addEventListener('dragleave',()=>drop.classList.remove('active'));
  drop.addEventListener('drop',event=>{
    event.preventDefault();
    drop.classList.remove('active');
    const input=drop.querySelector('input[type=file]');
    if(input&&event.dataTransfer.files.length){input.files=event.dataTransfer.files;input.dispatchEvent(new Event('change'));}
  });
});

const packetFiles=document.querySelector('#packet-files');
const attachmentGallery=document.querySelector('#attachment-gallery');
const attachmentControls=document.querySelector('#attachment-controls');
const fileOptions=document.querySelector('#file-options');
let attachmentState=[];
const isImage=file=>['image/png','image/jpeg','image/webp'].includes(file.type)||/\.(png|jpe?g|webp)$/i.test(file.name);
const syncFileOptions=()=>{
  if(!packetFiles||!fileOptions)return;
  fileOptions.value=JSON.stringify([...packetFiles.files].map((file,index)=>isImage(file)?attachmentState[index]||{mode:'analyze',instruction:''}:{mode:'analyze',instruction:''}));
};
const renderAttachments=()=>{
  if(!packetFiles||!attachmentGallery||!attachmentControls)return;
  attachmentGallery.replaceChildren();
  [...packetFiles.files].forEach((file,index)=>{
    if(!isImage(file))return;
    const state=attachmentState[index]||{mode:'analyze',instruction:''};attachmentState[index]=state;
    const card=document.createElement('div');card.className='attachment-card';
    const preview=document.createElement('img');preview.src=URL.createObjectURL(file);preview.alt=file.name;preview.onload=()=>URL.revokeObjectURL(preview.src);
    const name=document.createElement('strong');name.className='attachment-name';name.textContent=file.name;
    const label=document.createElement('label');const toggle=document.createElement('input');toggle.type='checkbox';toggle.checked=state.mode==='analyze';
    const caption=document.createElement('span');caption.textContent='Передать модели для анализа';label.append(toggle,caption);
    const instruction=document.createElement('input');instruction.type='text';instruction.placeholder='Что увидеть или извлечь? (необязательно)';instruction.value=state.instruction;instruction.disabled=!toggle.checked;
    toggle.addEventListener('change',()=>{state.mode=toggle.checked?'analyze':'store';instruction.disabled=!toggle.checked;syncFileOptions();});
    instruction.addEventListener('input',()=>{state.instruction=instruction.value;syncFileOptions();});
    card.append(preview,name,label,instruction);attachmentGallery.append(card);
  });
  attachmentControls.hidden=![...packetFiles.files].some(isImage);syncFileOptions();
};
if(packetFiles){
  packetFiles.addEventListener('change',()=>{attachmentState=[...packetFiles.files].map(()=>({mode:'analyze',instruction:''}));renderAttachments();});
  document.querySelectorAll('[data-all-mode]').forEach(button=>button.addEventListener('click',()=>{const mode=button.dataset.allMode;attachmentState.forEach((state,index)=>{if(isImage(packetFiles.files[index]))state.mode=mode;});renderAttachments();}));
}

const packetCards=[...document.querySelectorAll('[data-packet-id]')];
if(packetCards.length){
  const composer=document.querySelector('.packet-form');
  const composerIsEmpty=()=>composer&&[...composer.elements].every(element=>{
    if(element.type==='file')return !element.files.length;
    if(element.type==='hidden'||element.type==='button'||element.type==='submit')return true;
    return !('value' in element)||!element.value.trim();
  });
  const refreshPackets=async()=>{
    try{
      const response=await fetch('/api/packets',{cache:'no-store'});
      if(!response.ok)return;
      const packets=await response.json();
      let structuralChange=false;
      packets.forEach(packet=>{
        const card=document.querySelector(`[data-packet-id="${packet.id}"]`);
        if(!card)return;
        if(card.dataset.packetState!==packet.state)structuralChange=true;
        card.dataset.packetState=packet.state;
        const status=card.querySelector('[data-packet-status]');
        if(status)status.textContent=packet.state_label;
        const progress=card.querySelector('[data-packet-progress]');
        if(progress){progress.textContent=packet.progress;progress.hidden=!packet.progress;}
        const error=card.querySelector('[data-packet-error]');
        if(error){error.textContent=packet.last_error;error.hidden=!packet.last_error;}
        const attempts=card.querySelector('[data-attempt-count]');
        if(attempts&&Number(attempts.textContent)!==packet.attempt_count)structuralChange=true;
      });
      if(structuralChange&&composerIsEmpty())location.reload();
    }catch(_error){/* The local page remains usable while the server restarts. */}
  };
  setInterval(refreshPackets,2000);
}
