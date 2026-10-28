/** Display segmentation only: every line the model writes is one chat message. */
export function splitReply(value,{final=true}={}) {
  const text=String(value||'');const parts=[];let current='',fenced=false,inline=false;
  const emit=complete=>{const result=String(current).trim().replace(/[。！？!?…]+$/u,'');if(result)parts.push({text:result,complete});current='';};
  for(let i=0;i<text.length;i++){
    if(text.startsWith('```',i)){
      fenced=!fenced;current+='```';i+=2;continue;
    }
    const char=text[i];
    if(!fenced&&char==='`'){inline=!inline;current+=char;continue;}
    if(fenced||inline){current+=char;continue;}
    if(char==='\n'){emit(true);continue;}
    current+=char;
  }
  emit(final);
  return parts;
}
