/** Local square crop, shared by both identities. Only the confirmed 512px PNG is uploaded. */
export async function cropAvatar(file) {
  if(!file.type.startsWith('image/')||file.size>10*1024*1024)throw Error('请选择 10 MB 以内的图片');
  const bitmap=await createImageBitmap(file);
  return new Promise((resolve,reject)=>{
    const overlay=document.createElement('div');overlay.className='modal-backdrop avatar-crop-overlay';
    const dialog=document.createElement('section');dialog.className='modal avatar-crop-dialog';dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby','avatar-crop-title');
    const title=document.createElement('h3');title.id='avatar-crop-title';title.textContent='裁剪头像';
    const hint=document.createElement('p');hint.className='hint';hint.textContent='拖动图片调整位置，滑动缩放。确认后上传 512 × 512 的正方形头像。';
    const canvas=document.createElement('canvas');canvas.width=canvas.height=512;canvas.className='avatar-crop-canvas';canvas.setAttribute('aria-label','拖动图片调整裁剪位置');
    const controls=document.createElement('label');controls.className='avatar-crop-controls';controls.textContent='缩放';
    const slider=document.createElement('input');slider.type='range';slider.id='avatar-crop-zoom';slider.min=1;slider.max=4;slider.step=0.01;slider.value=1;
    const value=document.createElement('output');value.textContent='100%';controls.append(slider,value);
    const actions=document.createElement('div');actions.className='row end';
    const cancel=document.createElement('button');cancel.type='button';cancel.className='btn ghost';cancel.textContent='取消';
    const confirm=document.createElement('button');confirm.type='button';confirm.className='btn primary';confirm.id='avatar-crop-confirm';confirm.textContent='确认并上传';
    actions.append(cancel,confirm);dialog.append(title,hint,canvas,controls,actions);overlay.append(dialog);document.body.append(overlay);
    const previous=document.activeElement;const ctx=canvas.getContext('2d');let offsetX=0,offsetY=0,drag=null,closed=false;
    function draw(){
      const scale=Math.max(512/bitmap.width,512/bitmap.height)*Number(slider.value);
      const w=bitmap.width*scale,h=bitmap.height*scale;
      offsetX=Math.max(-(w-512)/2,Math.min((w-512)/2,offsetX));offsetY=Math.max(-(h-512)/2,Math.min((h-512)/2,offsetY));
      ctx.clearRect(0,0,512,512);ctx.drawImage(bitmap,(512-w)/2+offsetX,(512-h)/2+offsetY,w,h);value.textContent=Math.round(Number(slider.value)*100)+'%';
    }
    function close(result,error){if(closed)return;closed=true;overlay.remove();bitmap.close();document.removeEventListener('keydown',onKey,true);previous?.focus();if(error)reject(error);else resolve(result);}
    function onKey(event){
      if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();close(null);}
      if(event.key==='Tab'){
        const items=[slider,cancel,confirm],index=items.indexOf(document.activeElement);
        event.preventDefault();items[(index+(event.shiftKey?2:1)+3)%3].focus();
      }
    }
    document.addEventListener('keydown',onKey,true);
    slider.addEventListener('input',draw);
    canvas.addEventListener('pointerdown',event=>{if(event.button!==0)return;drag={x:event.clientX,y:event.clientY,offsetX,offsetY};canvas.setPointerCapture(event.pointerId);});
    canvas.addEventListener('pointermove',event=>{if(!drag)return;const scale=512/canvas.getBoundingClientRect().width;offsetX=drag.offsetX+(event.clientX-drag.x)*scale;offsetY=drag.offsetY+(event.clientY-drag.y)*scale;draw();});
    canvas.addEventListener('pointerup',()=>{drag=null;});canvas.addEventListener('pointercancel',()=>{drag=null;});
    cancel.addEventListener('click',()=>close(null));overlay.addEventListener('click',event=>{if(event.target===overlay)close(null);});
    confirm.addEventListener('click',()=>{
      confirm.disabled=true;
      canvas.toBlob(blob=>{if(!blob){close(null,Error('裁剪图片失败'));return;}close(new File([blob],file.name.replace(/\.[^.]*$/,'')+'-avatar.png',{type:'image/png'}));},'image/png');
    });
    draw();slider.focus();
  });
}
