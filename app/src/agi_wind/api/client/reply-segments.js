/** Display segmentation only: the archive/context/log keep the original full response. */
export function splitReply(value,{final=true}={}) {
  const text=String(value||'');const parts=[];let current='',fenced=false,inline=false;
  const emit=complete=>{const result=current.trim();if(result)parts.push({text:result,complete});current='';};
  for(let i=0;i<text.length;i++){
    if(text.startsWith('```',i)){
      fenced=!fenced;current+='```';i+=2;continue;
    }
    const char=text[i];
    if(!fenced&&char==='`'){inline=!inline;current+=char;continue;}
    if(fenced||inline){current+=char;continue;}
    const token=(current.match(/\S+$/)||[''])[0];
    const inUrl=/(?:https?:\/\/|www\.)/i.test(token);
    const boundary=char==='\n'||/[。！？]/.test(char)||(/[!?]/.test(char)&&!inUrl);
    if(!boundary){current+=char;continue;}
    if(char!=='\n'&&!current.replace(/^[\s?!！？。.…"'“”‘’（）()]+/u,'')){
      current+=char;continue; // Expressive question/exclamation prefixes belong to the following sentence.
    }
    if(char==='\n'){emit(true);continue;}
    // Strip sentence-ending punctuation, retain matching closing quotation/bracket marks.
    const family=char==='。'||char==='.'?'。.':char==='？'||char==='?'?'？?':'！!';
    while(i+1<text.length&&family.includes(text[i+1]))i++;
    if(/^[。！？.!?]+\s*$/.test(text.slice(i+1)))while(i+1<text.length&&/[。！？.!?]/.test(text[i+1]))i++;
    while(i+1<text.length&&/[”’"'）)】\]]/.test(text[i+1]))current+=text[++i];
    emit(true);
  }
  emit(final);
  return parts;
}
