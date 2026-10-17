/* Render a deliberately small Markdown subset. Raw HTML is always text. */
(function(root){
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(source,task){
 const tokens=[];
 const hold=html=>'\u0000'+(tokens.push(html)-1)+'\u0000';
 function inline(text){
  text=text.replace(/\u0000/g,'');
  text=text.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g,(_,label,url)=>{
   let path='';
   try{if(url.startsWith('file://')&&task?.path){const u=new URL(url),base=task.path.replace(/\/$/,'')+'/';const full=decodeURIComponent(u.pathname);if(!u.hostname&&full.startsWith(base))path=full.slice(base.length);}}catch{}
   if(path&&!path.split('/').includes('..'))return hold('<button class="source-reference" data-action="'+(path.endsWith('/')?'task-code':'open-workflow-file')+'" data-id="'+esc(task.id)+'" data-path="'+esc(path)+'">'+esc(label.replace(/`/g,''))+'</button>');
   if(/^https?:\/\//i.test(url))return hold('<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(label.replace(/`/g,''))+'</a>');
   return hold('<span title="Link unavailable">'+esc(label.replace(/`/g,''))+'</span>');
  });
  text=text.replace(/`([^`\n]+)`/g,(_,s)=>hold('<code>'+esc(s)+'</code>'));
  text=esc(text).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
  return text.replace(/\u0000(\d+)\u0000/g,(_,i)=>tokens[Number(i)]||'');
 }
 const lines=String(source).replace(/\r\n/g,'\n').split('\n');let html='',list='',paragraph=[];
 function flush(){if(paragraph.length){html+='<p>'+paragraph.map(inline).join('<br>')+'</p>';paragraph=[];}}
 function close(){if(list){html+='</'+list+'>';list='';}}
 for(let i=0;i<lines.length;i++){
  const line=lines[i];const fence=line.match(/^\s*```([\w+-]*)/);
  if(fence){flush();close();const code=[];while(++i<lines.length&&!/^\s*```\s*$/.test(lines[i]))code.push(lines[i]);html+='<div class="code-block"><div class="code-label"><span>'+esc(fence[1]||'code')+'</span><button data-action="copy-code">Copy code</button></div><pre><code>'+esc(code.join('\n'))+'</code></pre></div>';continue;}
  const heading=line.match(/^#{1,6}\s+(.+)/),item=line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)/);
  if(heading){flush();close();html+='<h3>'+inline(heading[1])+'</h3>';}
  else if(/^\s*---+\s*$/.test(line)){flush();close();html+='<hr>';}
  else if(item){flush();const kind=item[1]?'ul':'ol';if(list!==kind){close();const start=kind==='ol'?Number(line.trim().match(/^\d+/)[0]):1;html+='<'+kind+(start!==1?' start="'+start+'"':'')+'>';list=kind;}html+='<li>'+inline(item[2])+'</li>';}
  else if(!line.trim()){flush();close();}else{close();paragraph.push(line);}
 }
 flush();close();return html;
}
root.KokicMarkdown={render};if(typeof module!=='undefined')module.exports=root.KokicMarkdown;
})(globalThis);
