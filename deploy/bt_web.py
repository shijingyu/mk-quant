"""Register only MK Quant's static entry in the installed BaoTa panel."""
import json,os,re,sys
from pathlib import Path
name=os.environ.get('MK_PUBLIC_DOMAIN','').strip().lower()
if not name or len(name)>253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',name) or '.' not in name or any(not label or len(label)>63 or label.startswith('-') or label.endswith('-') for label in name.split('.')):
    raise SystemExit('Set MK_PUBLIC_DOMAIN to the domain you control before registering a site')
os.chdir('/www/server/panel')
sys.path.insert(0,'/www/server/panel')
sys.path.insert(0,'/www/server/panel/class')
import public
from projectModel.htmlModel import main
site=public.M('sites').where('name=?',(name,)).find()
if not site:
    args=public.dict_obj()
    args.path='/www/wwwroot/mk-quant/frontend/dist'
    args.webname=json.dumps({'domain':name,'domainlist':[],'count':0})
    args.port='80'
    args.ps='MK Quant 量化工作台'
    args.type_id=0
    result=main().create_project(args)
    if not result.get('siteStatus'):
        raise RuntimeError(result)
    site=public.M('sites').where('name=?',(name,)).find()
print(json.dumps({'name':name,'id':site['id'],'path':site['path'],'project_type':site['project_type']}))
