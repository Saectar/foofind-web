# -*- coding: utf-8 -*-
'''
    Funciones auxiliares
'''
from cgi import escape
from flask import request, g, Markup, url_for
from foofind.services import *
from foofind.utils import mid2hex, multipartition, logging
from foofind.utils.content_types import *
from foofind.utils.splitter import SEPPER, slugify
from flask.ext.babelex import gettext as _

__all__=(
    "FILTERS","DatabaseError","FileNotExist","FileRemoved","FileFoofindRemoved","FileUnknownBlock","FileNoSources",
    "highlight","extension_filename","url2filters","filters2url","fetch_global_data","prepare_args",
    "get_files","save_visited","comment_votes"
)

#constantes
FILTERS={
    "q":"",
    "type":["audio","video","image","document","software"],
    "src":{"download":'direct_downloads',"streaming":"Streaming","p2p":"P2P"},
    "size":['all_sizes','smaller_than','larger_than'],
    }


class DatabaseError(Exception):
    pass

class FileNotExist(Exception):
    pass

class FileRemoved(Exception):
    pass

class FileFoofindRemoved(Exception):
    pass

class FileUnknownBlock(Exception):
    pass

class FileNoSources(Exception):
    pass

def highlight(text_parts,match,limit_length=False):
    '''
    Resalta la parte de una cadena que coincide con otra dada
    '''
    separators = u"".join({i for i in match if i in SEPPER or i=="'"})
    parts = list(multipartition(match,separators))
    parts_type = [0 if part in separators else 1 if slugify(part) in text_parts else -1 for part in parts]
    parts_type.extend([0, 0])

    result = []
    strong = False
    for position, part in enumerate(parts):
        # abre tag si toca
        if not strong and parts_type[position]==1:
            result.append("<strong>")
            strong = True

        result.append(escape(part, True))

        # cierra tag si toca
        if strong and parts_type[position+1]<1 and not (parts_type[position+1]==0 and parts_type[position+2]==1):
            result.append("</strong>")
            strong = False

    result = u"".join(result)

    if limit_length: #si se quiere acortar el nombre del archivo para que aparezca al principio la primera palabra resaltada
        first_term = result.find("<strong>")
        first_part = None
        if first_term>15 and len(match)>25:
            first_part = result[:first_term]
            if first_part[-1]==" ": #hace que el ultimo espacio siempre se vea
                first_part = first_part[:-1]+u"&nbsp;"

        return (result,"<span>"+first_part+"</span>"+result[first_term:] if first_part else result)
    else:
        return result

def extension_filename(filename,ext):
    '''
    Añade la extension al nombre de archivo
    '''
    fn = Markup(filename).striptags()[:512]
    #si la extension no viene en el nombre se añade
    if ext and not fn.lower().endswith("."+ext.lower()):
        fn = fn+"."+ext
    return fn

def url2filters(urlfilters):
    '''
        Transforma los filtros que vienen a traves de la URL al formato interno
    '''
    has_changed = False
    if urlfilters:
        filters = dict(fil.split(":",1) for fil in urlfilters.split("/") if fil.find(":")!=-1)

        # borra parametros vacios o que no esten entre los filtros permitidos
        filters_keys= FILTERS.keys()
        for key in filters.keys():
            if key=="q" or key not in filters_keys or not filters[key]:
                del filters[key]
                has_changed = True

        if "src" in filters:
            if "," not in filters["src"]: #si puede ser un filtro antiguo
                srcs={"w":"download","f":"download","s":"streaming","e":"ed2k","t":"torrent","g":"gnutella","p":"p2p"}
                srcs_keys=srcs.keys()
                if filters["src"] in srcs_keys:
                    filters["src"]=srcs[filters["src"]]
                    has_changed=True
                elif all(True if source in srcs_keys else False for source in filters["src"]): #para las combinaciones de wfstge
                    filters["src"]=list(set([srcs[source] for source in filters["src"]])) #eliminar duplicados por si venia w y f
                    has_changed=True
                else:
                    filters["src"]=[filters["src"]]
            else:
                try:
                    filters['src'] = filters["src"].split(",")
                except:
                    del filters['src']
                    has_changed = True

        if "type" in filters:
            try:
                type_filter = []
                for atype in filters["type"].split(","):
                    if atype in CONTENTS_CATEGORY:
                        type_filter.append(atype)
                    elif atype.lower() in CONTENTS_CATEGORY:
                        type_filter.append(atype.lower())
                        has_changed = True
                    else:
                        has_changed = True

                filters['type'] = type_filter
            except:
                del filters['type']
                has_changed = True

        if "size" in filters:
            try:
                sizes = filters["size"].split(",")
                if len(sizes)==1 and 0<int(sizes[0])<5: # corrige tamaños antiguos
                    filters["size"] = [["0", "20"], ["0", "23"], ["0", "27"], ["27", "50"]][int(sizes[0])-1]
                    has_changed = True
                elif len(sizes)!=2 or not (sizes[0].isdigit() and sizes[1].isdigit() and int(sizes[0])>=0 and int(sizes[1])<=50): # valores incorrectos
                    del filters["size"]
                    has_changed = True
                else:
                    filters["size"] = sizes
            except:
                del filters["size"]
                has_changed = True
    else:
        filters = {}

    return filters, has_changed

def filters2url(filters):
    keys=FILTERS.keys()
    return "/".join(key+":"+(",".join(value) if isinstance(value, list) else value) for key, value in filters.iteritems() if value and key in keys and key!="q") or None

def fetch_global_data():
    '''
    Carga en g los datos de origenes y servidores de imagen.
    '''
    if hasattr(g,"sources"):
        return

    g.sources = {int(s["_id"]):s for s in filesdb.get_sources(blocked=None)}
    g.image_servers = filesdb.get_image_servers()

def prepare_args(query=None, filters=None):

    if query:
        args = filters.copy()
        args["q"] = query
        g.args=args
    else:
        g.args = args = {}

    #sources que se pueden elegir
    fetch_global_data()

    sources_streaming, sources_download, sources_p2p = searchd.get_sources_stats()

    g_sources_names = {}
    g_sources_streaming = []
    for src in sources_streaming:
        notld_src = src.split(".",1)[0].lower()
        g_sources_streaming.append(notld_src)
        g_sources_names[notld_src] = src
    g_sources_names["other-streamings"] = _("other_streamings")

    g_sources_download = []
    for src in sources_download:
        notld_src = src.split(".",1)[0].lower()
        g_sources_download.append(notld_src)
        g_sources_names[notld_src] = src
    g_sources_names["other-downloads"] = _("other_direct_downloads")

    g_sources_p2p = []
    for src in sources_p2p:
        notld_src = src.lower()
        g_sources_p2p.append(notld_src)
        g_sources_names[notld_src] = src

    g.sources_names = g_sources_names
    g.sources_streaming = g_sources_streaming
    g.sources_download = g_sources_download
    g.sources_p2p = g_sources_p2p

    g.visible_sources_streaming = visible_sources_streaming = g_sources_streaming[:8]+["other-streamings"]
    g.visible_sources_download = visible_sources_download = g_sources_download[:8]+["other-downloads"]

    g_active_srcs = set(args["src"]) if "src" in args else set()
    if "streaming" in g_active_srcs:
        g_active_srcs.update(set(visible_sources_streaming))
    elif any(src in visible_sources_streaming for src in g_active_srcs):
        g_active_srcs.add("streaming")

    if "download" in g_active_srcs:
        g_active_srcs.update(set(visible_sources_download))
    elif any(src in visible_sources_download for src in g_active_srcs):
        g_active_srcs.add("download")

    if "p2p" in g_active_srcs:
        g_active_srcs.update(set(g_sources_p2p))
    elif any(src in g_sources_p2p for src in g_active_srcs):
        g_active_srcs.add("p2p")

    g.active_srcs = g_active_srcs
    g.active_types = set(args["type"]) if "type" in args else set()

    g.search_url = url_for('files.search', query="")

def get_files(ids, sphinx_search=None):
    '''
    Recibe lista de tuplas de tamaño 3 o mayor (como las devueltas por search)
    y devuelve los ficheros del mongo correspondiente que no estén bloqueados.
    Si se recibe un fichero bloqueado, lo omite y bloquea en el sphinx.

    @type ids: iterable de tuplas de tamaño 3 o mayor
    @param ids: lista de tuplas (mongoid, id servidor, id sphinx)

    @yield: cada uno de los resultados de los ids en los mongos

    '''
    toblock = []
    for f in filesdb.get_files(ids, servers_known = True, bl = None):
        if f["bl"] == 0 or f["bl"] is None:
            yield f
        else:
            toblock.append((mid2hex(f["_id"]), str(f["s"])))

    # bloquea en sphinx los ficheros bloqueados
    if toblock and sphinx_search:
        cache.cacheme = False
        id_list = {i[0]:i for i in ids}
        sphinx_search.block_files([(id_list[mid][2],server, None, mid, id_list[mid][4]) for mid, server in toblock])

def save_visited(files):
    '''
    Recibe una lista de resultados de fill_data y guarda las url que sean de web
    '''
    if not g.search_bot and feedbackdb.initialized:
        result=[{"_id": data['urls'][0], "type":src} for afile in files for src, data in afile['view']['sources'].iteritems() if data['urls'] and data['icon'] in ['web','torrent']]

        if result:
            try:
                feedbackdb.visited_links(result)
            except:
                logging.error("Error saving visited links.")

def comment_votes(file_id,comment):
    '''
    Obtiene los votos de comentarios
    '''
    return {}

    comment_votes={}
    if "vs" in comment:
        for i,comment_vote in enumerate(usersdb.get_file_comment_votes(file_id)):
            if not comment_vote["_id"] in comment_votes:
                comment_votes[comment_vote["_id"][0:40]]=[0,0,0]

            if comment_vote["k"]>0:
                comment_votes[comment_vote["_id"][0:40]][0]+=1
            else:
                comment_votes[comment_vote["_id"][0:40]][1]+=1

            #si el usuario esta logueado y ha votado se guarda para mostrarlo activo
            if current_user.is_authenticated() and comment_vote["u"]==current_user.id:
                comment_votes[comment_vote["_id"][0:40]][2]=comment_vote["k"]

    return comment_votes
