/** Authorized image attachments and an accessible lightbox. */
function node(tag, className, text) {
  const value=document.createElement(tag);if(className)value.className=className;
  if(text!==undefined)value.textContent=text;return value;
}

export function showImage(url,name='图片') {
  const overlay=node('div','image-lightbox');overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.setAttribute('aria-label',name);
  const image=node('img');image.src=url;image.alt=name;
  const close=node('button','btn','关闭');close.type='button';const previous=document.activeElement;
  const dismiss=()=>{overlay.remove();document.removeEventListener('keydown',key);previous?.focus();};
  const key=event=>{if(event.key==='Escape')dismiss();if(event.key==='Tab'){event.preventDefault();close.focus();}};
  close.addEventListener('click',dismiss);overlay.addEventListener('click',event=>{if(event.target===overlay)dismiss();});
  overlay.append(close,image);document.body.append(overlay);document.addEventListener('keydown',key);close.focus();
}

export function appendImages(client,container,attachments=[]) {
  for(const attachment of attachments) {
    if(!attachment.mime?.startsWith('image/')||container.querySelector(`[data-asset-id="${CSS.escape(attachment.asset_id)}"]`))continue;
    const button=node('button','image-attachment');button.type='button';button.dataset.assetId=attachment.asset_id;
    const image=node('img');image.alt=attachment.name||'图片';image.loading='lazy';
    image.width=attachment.width||240;image.height=attachment.height||240;
    button.append(image);container.append(button);
    client.assetObjectUrl(attachment.asset_id).then(url=>{if(!button.isConnected)return;image.src=url;button.addEventListener('click',()=>showImage(url,image.alt));}).catch(()=>{button.textContent='图片读取失败';});
  }
}

export function messageImages(client,message,attachments) {
  if(!message.images){message.images=node('div','message-images');message.body.after(message.images);}
  appendImages(client,message.images,attachments);
}
