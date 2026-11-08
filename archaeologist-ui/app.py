import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from pipeline import run, repository, export_zip, TYPES
from configuration import enrich,validate_config
from git_collect import supplement

BASE=Path(__file__).resolve().parent
class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('GitHub 项目考古学家 · 数据工作台 · Git补采版 2026.11.02'); self.geometry('1180x780'); self.minsize(960,640)
        self.bus=queue.Queue(); self.cancel=threading.Event(); self.worker=None; self.current=None; self.resume=None
        self.configure(bg='#f4f6fa'); style=ttk.Style(self); style.theme_use('clam'); style.configure('TFrame',background='#f4f6fa'); style.configure('TLabel',background='#f4f6fa',font=('Microsoft YaHei UI',10)); style.configure('Title.TLabel',font=('Microsoft YaHei UI',22,'bold')); style.configure('TButton',padding=8); style.configure('Treeview',rowheight=27)
        outer=ttk.Frame(self,padding=22); outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='项目数据工作台',style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer,text='输入仓库 → 采集与清洗 → 查看实体关系 → 导出图谱交接包').pack(anchor='w',pady=(4,8))
        actions=ttk.Frame(outer);actions.pack(fill='x',pady=(0,12))
        ttk.Button(actions,text='打开已有结果',command=self.open_result).pack(side='left',padx=(0,8))
        self.git_btn=ttk.Button(actions,text='为当前结果补采本地 Git',command=self.collect_git)
        self.git_btn.pack(side='left',padx=(0,10))
        ttk.Label(actions,text='Git补采版 · 已有数据从这里操作，无需重跑API',foreground='#276b9b').pack(side='left')
        body=ttk.Frame(outer); body.pack(fill='both',expand=True)
        panel=ttk.Frame(body,width=300);panel.pack(side='left',fill='y');panel.pack_propagate(False)
        canvas=tk.Canvas(panel,width=278,bg='#f4f6fa',highlightthickness=0);scroll=ttk.Scrollbar(panel,orient='vertical',command=canvas.yview);canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        left=ttk.Frame(canvas,padding=(0,0,12,0));window=canvas.create_window((0,0),window=left,anchor='nw')
        left.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')));canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        self.url=tk.StringVar(value='https://github.com/rasbt/mlxtend'); self.token=tk.StringVar(value=os.environ.get('GITHUB_TOKEN','')); self.out=tk.StringVar(value=str(BASE/'data'))
        for text,var,mask in [('仓库链接',self.url,''),('GitHub Token（仅本次使用）',self.token,'•'),('结果保存目录',self.out,'')]:
            ttk.Label(left,text=text).pack(anchor='w',pady=(8,4)); ttk.Entry(left,textvariable=var,show=mask).pack(fill='x')
        ttk.Button(left,text='选择保存目录',command=self.choose).pack(fill='x',pady=7)
        ttk.Label(left,text='全量采集',font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w',pady=(12,5))
        ttk.Label(left,text='自动读取全部可访问的Issue、PR、评论和发布记录，采集分支/tag历史及当前源码。无需设置数量。',wraplength=245).pack(anchor='w')
        ttk.Label(left,text='任务可能持续较久；遇到限流可取消并恢复。全量范围与未实现阶段会写入报告。',wraplength=245,foreground='#65758a').pack(anchor='w',pady=8)
        self.with_git=tk.BooleanVar(value=True)
        ttk.Checkbutton(left,text='API完成后补采本地Git历史',variable=self.with_git).pack(anchor='w',pady=5)
        self.start_btn=ttk.Button(left,text='开始采集与处理',command=self.start); self.start_btn.pack(fill='x',pady=(18,5))
        ttk.Button(left,text='取消任务',command=self.cancel.set).pack(fill='x',pady=3)
        ttk.Button(left,text='恢复已有任务',command=self.restore).pack(fill='x',pady=3)
        ttk.Button(left,text='打开已有结果',command=self.open_result).pack(fill='x',pady=3)
        ttk.Button(left,text='导出 ZIP 交接包',command=self.export).pack(fill='x',pady=3)
        ttk.Label(left,text='采集后可在“模块与功能”页补充人工配置。语义抽取与历史函数映射尚未启用。',wraplength=245,foreground='#65758a').pack(anchor='w',pady=14)
        right=ttk.Frame(body); right.pack(side='left',fill='both',expand=True)
        self.status=tk.StringVar(value='准备就绪 · 全量采集模式'); ttk.Label(right,textvariable=self.status,wraplength=770).pack(anchor='w')
        self.progress=ttk.Progressbar(right,mode='indeterminate'); self.progress.pack(fill='x',pady=10)
        self.progress_text=tk.StringVar(value='等待开始');ttk.Label(right,textvariable=self.progress_text,wraplength=720).pack(anchor='w')
        self.runtime_text=tk.StringVar(value='耗时 00:00 · 新请求 0 · 缓存复用 0');ttk.Label(right,textvariable=self.runtime_text,foreground='#65758a').pack(anchor='w',pady=(3,8))
        self.overview=tk.StringVar(value='尚未选择结果');ttk.Label(right,textvariable=self.overview,foreground='#276b9b',wraplength=720).pack(anchor='w',pady=(0,8))
        tabs=ttk.Notebook(right); tabs.pack(fill='both',expand=True)
        self.log=tk.Text(tabs,wrap='word',font=('Consolas',10),state='disabled'); tabs.add(self.log,text='任务进度')
        preview=ttk.Frame(tabs,padding=10); tabs.add(preview,text='实体 / 关系预览')
        self.selection=tk.StringVar(value='Issue'); combo=ttk.Combobox(preview,textvariable=self.selection,values=list(TYPES)+['关系'],state='readonly'); combo.pack(anchor='w'); combo.bind('<<ComboboxSelected>>',lambda _:self.preview())
        searchbar=ttk.Frame(preview);searchbar.pack(fill='x',pady=5);self.search=tk.StringVar();ttk.Entry(searchbar,textvariable=self.search).pack(side='left',fill='x',expand=True);ttk.Button(searchbar,text='搜索',command=self.preview).pack(side='left');self.search.trace_add('write',lambda *_:self.preview())
        self.count_text=tk.StringVar();ttk.Label(preview,textvariable=self.count_text).pack(anchor='w')
        self.table=ttk.Treeview(preview,columns=('type','id'),show='headings',height=8); self.table.heading('type',text='类型'); self.table.heading('id',text='标识 / 关系'); self.table.column('type',width=130); self.table.column('id',width=500); self.table.pack(fill='both',expand=True,pady=8); self.table.bind('<<TreeviewSelect>>',self.detail)
        self.details=tk.Text(preview,height=12,wrap='word',font=('Consolas',10),state='disabled'); self.details.pack(fill='both',expand=True)
        ttk.Button(preview,text='查看选中记录的原始证据',command=self.show_evidence).pack(anchor='w')
        self.report=tk.Text(tabs,wrap='word',font=('Consolas',10),state='disabled'); tabs.add(self.report,text='质量与交接状态')
        self.config={'repository':'rasbt/mlxtend','modules':[],'features':[]}; self.config_rows={}
        config_tab=ttk.Frame(tabs,padding=12);tabs.add(config_tab,text='模块与功能')
        ttk.Label(config_tab,text='先打开采集结果，再配置。路径只匹配已采文件；功能种子填写完整CodeSymbol ID。',wraplength=700).pack(anchor='w')
        form=ttk.Frame(config_tab);form.pack(fill='x',pady=8)
        self.config_kind=tk.StringVar(value='Module');ttk.Combobox(form,textvariable=self.config_kind,values=['Module','Feature'],state='readonly',width=15).grid(row=0,column=1,sticky='w');ttk.Label(form,text='配置类型').grid(row=0,column=0,sticky='w')
        self.config_fields={}
        for i,(key,label) in enumerate([('name','名称'),('description','职责 / 功能说明'),('values','模块路径 / 功能种子（逗号分隔）')],1):
            ttk.Label(form,text=label).grid(row=i,column=0,sticky='w',pady=4);var=tk.StringVar();ttk.Entry(form,textvariable=var).grid(row=i,column=1,sticky='ew');self.config_fields[key]=var
        form.columnconfigure(1,weight=1)
        buttons=ttk.Frame(config_tab);buttons.pack(fill='x')
        for title,callback in [('添加 / 更新',self.add_config),('移除选中',self.remove_config),('导入配置',self.import_config),('保存配置',self.save_config)]:ttk.Button(buttons,text=title,command=callback).pack(side='left',padx=2)
        self.config_table=ttk.Treeview(config_tab,columns=('kind','name','values'),show='headings',height=8)
        for col,label in [('kind','类型'),('name','名称'),('values','路径 / 种子')]:self.config_table.heading(col,text=label);self.config_table.column(col,width=160)
        self.config_table.pack(fill='both',expand=True,pady=10)
        ttk.Button(config_tab,text='应用配置，生成新的交接结果',command=self.apply_config).pack(anchor='w')
        ttk.Label(config_tab,text='原结果不被覆盖。没有匹配文件的模块会提示；未填写种子的功能不生成实现关系。',wraplength=700).pack(anchor='w',pady=8)
        history=ttk.Frame(tabs,padding=12);tabs.add(history,text='任务历史')
        ttk.Button(history,text='刷新保存目录的任务记录',command=self.refresh_history).pack(anchor='w')
        self.history=ttk.Treeview(history,columns=('repo','state','run'),show='headings')
        for col,label in [('repo','仓库'),('state','状态'),('run','任务编号')]:self.history.heading(col,text=label);self.history.column(col,width=180)
        self.history.pack(fill='both',expand=True,pady=10);self.history_paths={};self.history.bind('<Double-1>',self.load_history)
        ttk.Label(history,text='双击打开结果。恢复采集仍从左侧“恢复已有任务”操作。').pack(anchor='w')
        self.items={}; self.after(100,self.poll); self.protocol('WM_DELETE_WINDOW',self.close)
    def choose(self):
        p=filedialog.askdirectory()
        if p:self.out.set(p)
    def busy(self): return self.worker and self.worker.is_alive()
    def start(self):
        if self.busy():return
        try:
            repository(self.url.get()); limits={k:None for k in ('issues','prs','commits','releases','comments','source_files')}
        except Exception as e:messagebox.showerror('检查输入',str(e));return
        self.cancel.clear();self.current=None;self.fill(self.log,'');self.fill(self.details,''); self.start_btn.configure(state='disabled'); self.progress.configure(mode='indeterminate',value=0);self.progress.start(); self.status.set('正在启动采集…');self.progress_text.set('正在发现任务对象，暂未确定数量')
        url,token,out,resume=self.url.get(),self.token.get().strip(),self.out.get(),self.resume; self.resume=None
        with_git=self.with_git.get()
        def work():
            try:
                def notify(k,v):
                    if k!='done':self.bus.put((k,v))
                root=run(url,token,out,limits,notify,self.cancel,resume)
                if with_git and not self.cancel.is_set():
                    try:root=supplement(root,notify=notify,cancel=self.cancel)
                    except Exception as e:self.bus.put(('log','Git补采未完成，API结果已保存：'+str(e)))
                self.bus.put(('done',str(root)))
            except Exception as e:self.bus.put(('fatal',str(e)))
        self.worker=threading.Thread(target=work,daemon=True);self.worker.start()
    def poll(self):
        try:
            for _ in range(150):
                kind,value=self.bus.get_nowait()
                if kind=='log':self.fill(self.log,value+'\n',append=True)
                elif kind=='stage':self.status.set(value)
                elif kind=='progress':
                    self.progress.stop();self.progress.configure(mode='determinate',maximum=max(value['total'],1),value=value['done'])
                    self.progress_text.set(f"{value['label']}：已处理 {value['done']} / {value['total']}（当前阶段）")
                elif kind=='telemetry':
                    seconds=value['elapsed'];self.runtime_text.set(f"本次运行 {seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d} · 新请求 {value['network']} · 缓存复用 {value['cache']}")
                elif kind=='exported':
                    self.progress.stop();self.start_btn.configure(state='normal');self.status.set('交接包已导出：'+value);messagebox.showinfo('导出成功',value)
                elif kind=='configured':
                    self.progress.stop();self.start_btn.configure(state='normal');self.load(Path(value))
                elif kind in ('done','fatal'):
                    self.progress.stop();self.start_btn.configure(state='normal')
                    self.progress_text.set('处理已结束，完整性以质量报告为准' if kind=='done' else '任务异常结束')
                    if kind=='done':self.load(Path(value))
                    else:self.status.set('任务失败：'+value)
        except queue.Empty:pass
        self.after(100,self.poll)
    def fill(self,widget,text,append=False):
        widget.configure(state='normal')
        if not append:widget.delete('1.0','end')
        widget.insert('end',text);widget.see('end');widget.configure(state='disabled')
        if append:
            widget.configure(state='normal')
            count=int(widget.index('end-1c').split('.')[0])
            if count>1200:widget.delete('1.0',f'{count-1000}.0')
            widget.configure(state='disabled')
    def load(self,p):
        try:
            manifest=json.loads((p/'manifest.json').read_text(encoding='utf-8')); quality=json.loads((p/'reports/quality.json').read_text(encoding='utf-8'))
            self.current=p; labels={'ready':'声明范围已处理','partial':'部分覆盖 / 存在待处理项','cancelled':'已取消并保存'}
            self.status.set(labels.get(manifest['state'],manifest['state'])+'  |  '+str(p))
            self.overview.set(f"实体 {sum(quality['node_counts'].values())}  ·  关系 {sum(quality['relation_counts'].values())}  ·  错误 {len(quality['errors'])}  ·  隔离 {quality.get('rejected_records',0)}  ·  语义抽取未运行")
            self.fill(self.report,json.dumps({'manifest':manifest,'quality':quality},ensure_ascii=False,indent=2)); self.preview()
            config_path=p/'config/project.json'
            self.config=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {'repository':manifest['canonical_repo'],'modules':[],'features':[]}
            self.refresh_config()
        except Exception as e:messagebox.showerror('无法打开结果',str(e))
    def open_result(self):
        if self.busy():messagebox.showinfo('提示','请先等待当前任务结束或取消');return
        p=filedialog.askdirectory(title='选择包含manifest.json的任务目录')
        if p:self.load(Path(p))
    def restore(self):
        if self.busy():return
        p=filedialog.askdirectory(title='选择已有任务目录（复用缓存重新处理）')
        if not p:return
        try:
            job=json.loads((Path(p)/'job.json').read_text(encoding='utf-8'));self.url.set(job['url'])
            self.resume=p; self.start()
        except Exception as e:messagebox.showerror('恢复失败',str(e))
    def preview(self):
        self.table.delete(*self.table.get_children());self.items={}
        if not self.current:return
        kind=self.selection.get(); file=self.current/('processed/relations.jsonl' if kind=='关系' else 'processed/nodes/'+TYPES[kind]+'.jsonl')
        try:
            matches=0
            with file.open(encoding='utf-8') as stream:
                for line in stream:
                    if self.search.get().casefold() not in line.casefold():continue
                    matches+=1
                    if matches>1000:continue
                    row=json.loads(line); key=self.table.insert('','end',values=(row.get('entity_type',row.get('relation')),row.get('id') or row['source_id']+' → '+row['target_id']));self.items[key]=row
            self.count_text.set(f'匹配 {matches} 条 · 显示前 {min(matches,1000)} 条（可搜索缩小范围）')
            if not matches:self.fill(self.details,'此类型没有已接收记录，或没有匹配搜索。未执行类型请查看质量与交接状态。')
        except Exception as e:self.fill(self.details,str(e))
    def detail(self,_):
        selected=self.table.selection()
        if selected:self.fill(self.details,json.dumps(self.items[selected[0]],ensure_ascii=False,indent=2))
    def show_evidence(self):
        selected=self.table.selection()
        if not selected or not self.current:return
        row=self.items[selected[0]];ids=set(row.get('evidence_ids',row.get('provenance',{}).get('evidence_ids',[])));records=[]
        try:
            for line in (self.current/'processed/evidence.jsonl').read_text(encoding='utf-8').splitlines():
                record=json.loads(line)
                if record['evidence_id'] in ids:
                    raw=(self.current/record['raw_ref']).resolve()
                    if not raw.is_relative_to(self.current.resolve()):raise ValueError('证据路径超出结果目录')
                    if raw.suffix=='.jsonl':
                        sha=record.get('source_url','').rsplit('/',1)[-1]
                        payload=[]
                        with raw.open(encoding='utf-8') as stream:
                            for text in stream:
                                item=json.loads(text)
                                if item.get('sha')==sha or item.get('commit_sha')==sha:payload.append(item)
                    else:payload=json.loads(raw.read_text(encoding='utf-8'))
                    records.append({'evidence':record,'raw':payload})
            self.fill(self.details,json.dumps(records,ensure_ascii=False,indent=2) if records else '没有找到对应证据')
        except Exception as e:self.fill(self.details,'证据读取失败：'+str(e))
    def export(self):
        if not self.current:messagebox.showinfo('提示','请先完成任务或打开已有结果');return
        if self.busy():messagebox.showinfo('提示','请等待任务结束后再导出');return
        root=self.current
        self.background(lambda:export_zip(root),'exported','正在打包交接数据…')
    def collect_git(self):
        if self.busy():return
        if not self.current:
            self.open_result()
            if not self.current:return
        source=self.current;self.cancel.clear()
        self.background(lambda:supplement(source,notify=lambda k,v:self.bus.put((k,v)),cancel=self.cancel),
                        'done','正在补采本地Git，复用已有API数据…')
    def background(self,fn,event,label):
        if self.busy():return
        self.start_btn.configure(state='disabled');self.progress.configure(mode='indeterminate',value=0);self.progress.start();self.status.set(label)
        def work():
            try:self.bus.put((event,str(fn())))
            except Exception as e:self.bus.put(('fatal',str(e)))
        self.worker=threading.Thread(target=work,daemon=True);self.worker.start()
    def refresh_config(self):
        self.config_table.delete(*self.config_table.get_children());self.config_rows={}
        for group in ('modules','features'):
            for item in self.config.get(group,[]):
                key=self.config_table.insert('','end',values=(group,item['name'],', '.join(item.get('path_prefixes',item.get('seed_symbols',[])))))
                self.config_rows[key]=(group,item['name'])
    def add_config(self):
        try:
            name=self.config_fields['name'].get().strip();desc=self.config_fields['description'].get().strip();values=[v.strip() for v in self.config_fields['values'].get().split(',') if v.strip()]
            group='modules' if self.config_kind.get()=='Module' else 'features';field='path_prefixes' if group=='modules' else 'seed_symbols'
            candidate=json.loads(json.dumps(self.config));candidate[group]=[x for x in candidate[group] if x['name']!=name]+[{'name':name,'description':desc,field:values}]
            validate_config(candidate);self.config=candidate;self.refresh_config()
        except Exception as e:messagebox.showerror('配置检查',str(e))
    def remove_config(self):
        for key in self.config_table.selection():
            group,name=self.config_rows[key];self.config[group]=[x for x in self.config[group] if x['name']!=name]
        self.refresh_config()
    def import_config(self):
        p=filedialog.askopenfilename(filetypes=[('配置JSON','*.json')])
        if p:
            try:self.config=validate_config(json.loads(Path(p).read_text(encoding='utf-8')));self.refresh_config()
            except Exception as e:messagebox.showerror('配置导入失败',str(e))
    def save_config(self):
        p=filedialog.asksaveasfilename(defaultextension='.json',filetypes=[('配置JSON','*.json')])
        if p:Path(p).write_text(json.dumps(self.config,ensure_ascii=False,indent=2),encoding='utf-8')
    def apply_config(self):
        if self.busy():return
        if not self.current:messagebox.showinfo('提示','请先完成采集或打开已有结果');return
        if not self.config['modules'] and not self.config['features']:messagebox.showinfo('提示','请至少添加一项配置');return
        root=self.current;config=json.loads(json.dumps(self.config));self.background(lambda:enrich(root,config),'configured','正在校验配置并生成新结果…')
    def refresh_history(self):
        if self.busy():return
        self.history.delete(*self.history.get_children());self.history_paths={}
        root=Path(self.out.get())
        if not root.exists():return
        for f in sorted(root.glob('*/*/manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
            try:
                data=json.loads(f.read_text(encoding='utf-8'));key=self.history.insert('','end',values=(data['canonical_repo'],data['state'],data['run_id']));self.history_paths[key]=f.parent
            except (OSError,ValueError,KeyError):continue
    def load_history(self,_):
        if self.busy():return
        selected=self.history.selection()
        if selected:self.load(self.history_paths[selected[0]])
    def close(self):
        if self.busy():
            if not messagebox.askyesno('退出','任务仍在运行。退出会中断处理；原始缓存可恢复。是否退出？'):return
            self.cancel.set()
        self.destroy()
if __name__=='__main__':App().mainloop()
