const search=document.querySelector('#search');
if(search)search.addEventListener('input',()=>{
  const q=search.value.toLowerCase();
  document.querySelectorAll('#pages .card').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(q));
});

document.querySelectorAll('.drop').forEach(drop=>{
  drop.addEventListener('dragover',event=>{event.preventDefault();drop.classList.add('active')});
  drop.addEventListener('dragleave',()=>drop.classList.remove('active'));
  drop.addEventListener('drop',event=>{
    event.preventDefault();
    drop.classList.remove('active');
    const input=drop.querySelector('input[type=file]');
    if(input&&event.dataTransfer.files.length)input.files=event.dataTransfer.files;
  });
});

const packetCards=[...document.querySelectorAll('[data-packet-id]')];
if(packetCards.length){
  const composer=document.querySelector('.packet-form');
  const composerIsEmpty=()=>composer&&[...composer.elements].every(element=>{
    if(element.type==='file')return !element.files.length;
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
