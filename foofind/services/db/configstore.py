# -*- coding: utf-8 -*-

import pymongo
import pymongo.errors
import time
import memcache
import socket

from foofind.utils.fooprint import ManagedSelect, ParamSelector, DecoratedView
from foofind.utils import check_capped_collections, logging
from foofind.services.extensions import cache

class ConfigStore(object):
    '''
    Gestor de configuraciones en base de datos
    '''
    _capped = {
        "actions":1000,
        }
    def __init__(self):
        self._action_handlers = {}
        self._actions_lt = time.time() # No se ejecutan las acciones previas al despliegue
        self._alternatives_lt = 0 # La configuración se carga en el despliegue
        self._alternatives_skip = set()

    def init_app(self, app):
        '''
        Apply config database access configuration.

        @param app: Flask application.
        '''
        self._views = {
            endpoint: view_fnc
            for endpoint, view_fnc in app.view_functions.iteritems()
            if isinstance(view_fnc, DecoratedView) and isinstance(view_fnc.select, ManagedSelect)
            }
        self._appid = app.config["APPLICATION_ID"]

        if app.config["DATA_SOURCE_CONFIG"]:
            if "DATA_SOURCE_CONFIG_RS" in app.config:
                self.config_conn = pymongo.MongoReplicaSetClient(app.config["DATA_SOURCE_CONFIG"],
                                                                max_pool_size=app.config["DATA_SOURCE_MAX_POOL_SIZE"],
                                                                replicaSet = app.config["DATA_SOURCE_CONFIG_RS"],
                                                                read_preference = pymongo.read_preferences.ReadPreference.SECONDARY_PREFERRED,
                                                                tag_sets = app.config.get("DATA_SOURCE_CONFIG_RS_TAG_SETS",[{}]),
                                                                secondary_acceptable_latency_ms = app.config.get("SECONDARY_ACCEPTABLE_LATENCY_MS", 15))
            else:
                self.config_conn = pymongo.MongoClient(app.config["DATA_SOURCE_CONFIG"], max_pool_size=app.config["DATA_SOURCE_MAX_POOL_SIZE"], slave_okay=True)

            self.init_config_conn()

    def share_connections(self, config_conn=None):
        '''
        Allows to share data source connections with other modules.
        '''
        if config_conn:
            self.config_conn = config_conn
            self.init_config_conn()

    def init_config_conn(self):
        '''
        Allows to share data source connections with other modules.
        '''
        check_capped_collections(self.config_conn.config, self._capped)

        # Guardamos el timestamp de la última tarea enviada para ignorar
        for i in self.config_conn.config.actions.find().sort("lt", -1).limit(1):
            self._actions_lt = i["lt"]

        # Registramos este perfil de aplicación
        self.config_conn.config.profiles.save({"_id": self._appid, "lt": time.time()})
        self.config_conn.end_request()

    def get_current_profiles(self):
        '''
        Obtiene los application_ids de las instancias registradas
        '''
        tr = [doc["_id"] for doc in self.config_conn.config.profiles.find()]
        self.config_conn.end_request()
        return tr

    def get_counter(self, counterid, amount=1):
        '''
        Obtiene el valor para el contador con el ID dado

        @type counterid: str o unicode
        @param counterid: Identificador único del contador

        @type amount: int (opcional)
        @param amount: incr

        @rtype int
        @return Valor del contador, autoincrementado
        '''
        doc = self.config_conn.config.counters.find_and_modify(
            {"_id": counterid},
            {"$inc": {"n": amount}},
            upsert = True)
        return doc.get("n", 0)

    def pull(self):
        '''
        Descarga la configuración de la base de datos y
        actualiza la configuración local.
        '''
        self.pull_actions()
        self.pull_alternatives()

    def register_action(self, actionid, fnc, *args, **kwargs):
        '''
        Registra una función con un id de acción y parámetros.

        @type actionid: basestring
        @param actionid: Identificación de tarea.

        @type fnc: callable
        @param fnc: Función a realizar

        @type _unique: bool
        @param _unique: Parámetro que exigue que la tarea se ejecute una sóla
                        vez de entre todas las instancias.
                        False por defecto.

        @args, kwargs
        '''
        assert isinstance(actionid, basestring)
        self._action_handlers[actionid] = (fnc, kwargs.pop("_unique", False), args, kwargs)

    def run_action(self, actionid, target="*"):
        '''
        Marca en el servidor la acción a realizar. Admite determinar el
        tipo de servidor (según su APPLICATION_ID) que va a realizar la
        operación.

        @type actionid: str
        @param actionid: Identificador de acción tal cual ha sido registrada.

        @type target: str
        @param target: Identificador correspondiente al APPLICATION_ID de la
                       configuración de la aplicación que deberá procesar la
                       operación. Por defecto es "*" (todas).

        '''
        now = time.time()
        self.config_conn.config.actions.save({"actionid":actionid, "target":target, "lt":now})
        self.config_conn.end_request()

    def action(self, actionid, *args, **kwargs):
        '''
        Decorador que registra una función con un id de acción y parámetros,
        tiene el mismo efecto que `register_action`.

        @type actionid: str
        @param actionid: identificador de acción

        @return Función sin modificar
        '''
        def decorator(f):
            self.register_action(actionid, f, *args, **kwargs)
            return f
        return f

    def pull_actions(self):
        '''
        Descarga la configuración de acciones y las ejecuta.
        '''
        query = {
            "lt": { "$gt": self._actions_lt },
            "target": {"$in": (self._appid, "*")},
            "actionid": {"$in": [ action[0] for action in self.list_actions() if action[2] ]},
            }
        last = self._actions_lt
        # Tareas que sólo deben realizarse una vez
        while True:
            try:
                # Operación atómica: obtiene y cambia timestamp para que no se repita
                action = self.config_conn.config.actions.find_and_modify(query, update={"$set":{"lt":0}})
            except pymongo.errors.AutoReconnect as e:
                action = None
                logging.exception("Can't access to config database.")
            if action is None: break
            if action["lt"] > last:
                last = action["lt"]
            actionid = action["actionid"]
            if actionid in self._action_handlers:
                fnc, unique, args, kwargs = self._action_handlers[actionid]
                fnc(*args, **kwargs)
        # Tareas a realizar en todas las instancias
        del query["actionid"]

        try:
            for action in self.config_conn.config.actions.find(query):
                if action["lt"] > last:
                    last = action["lt"]
                actionid = action["actionid"]
                if actionid in self._action_handlers:
                    fnc, unique, args, kwargs = self._action_handlers[actionid]
                    fnc(*args, **kwargs)
            self._actions_lt = last
        except pymongo.errors.AutoReconnect as e:
            logging.exception("Can't access to config database.")
        except BaseException as e:
            logging.exception("Error running action.")
        finally:
            self.config_conn.end_request()

    def list_actions(self):
        '''
        Lista las acciones registradas

        @yields tupla con el identificador de acción, la función, el iterable
                de argumentos y el diccionario de argumentos con nombre.
        '''
        for k, (f, unique, args, kwargs) in self._action_handlers.iteritems():
            yield k, f, unique, args, kwargs

    def pull_alternatives(self):
        '''
        Descarga la configuración de alternativas del servidor
        y la aplica.
        '''
        last = 0
        try:
            for alternative in self.config_conn.config.alternatives.find({
              "_id": { "$in": self._views.keys() },
              "lt" : { "$gt": self._alternatives_lt }
              }):
                endpoint = alternative["_id"]
                if endpoint in self._alternatives_skip:
                    self._alternatives_skip.remove(endpoint)
                elif endpoint in self._views and "config" in alternative:
                    if "probability" in alternative["config"]:
                        # Probability se guarda como lista por limitaciones de mongodb
                        alternative["config"]["probability"] = dict(alternative["config"]["probability"])
                    self._views[endpoint].select.config(alternative["config"])
                if alternative.get("lt", 0) > last:
                    last = alternative["lt"]
            self._alternatives_lt = max(self._alternatives_lt, last)
            self.config_conn.end_request()
        except pymongo.errors.AutoReconnect as e:
            logging.exception("Can't access to config database.")

    def remove_alternative(self, altid):
        '''
        Borra la configuración de alternativa con id del servidor

        @type altid: str
        @param altid: identificador de alternativa
        '''
        self.config_conn.config.alternatives.remove({"_id":altid})
        self.config_conn.end_request()

    def list_alternatives(self, skip=None, limit=None):
        '''
        Devuelve la lista de alternativas disponibles localmente o en la
        configuración del servidor. Soporta paginación

        @type skip: None o int
        @param skip: número de registros a ignorar (paginación)

        @type limit: None o int
        @param limit: número máximo de registros a retornar (paginación)

        @rtype list
        @return lista de ids de alternativas
        '''
        tr = []
        ids = []
        cur = self.config_conn.config.alternatives.find().sort("_id")
        if not skip is None: cur.skip(skip)
        if not limit is None: cur.limit(limit)
        for r in cur:
            a = r["config"]
            a["_id"] = r["_id"]
            ids.append(r["_id"])
            tr.append(a)
        self.config_conn.end_request()
        tr.extend({"_id": k, "methods": ", ".join(v.select.methods)} for k, v in self._views.iteritems() if k not in ids)
        tr.sort(key=lambda x:x["_id"])
        if skip:
            skip = min(len(self._views), skip)
            return tr[skip:skip+limit] if limit else tr[skip:]
        elif limit:
            return tr[:limit]
        return tr

    def count_alternatives(self):
        '''
        Devuelve el número de alternativas disponibles localmente o en la
        configuración del servidor.

        @rtype int
        @return número de alternativas
        '''
        tr = self.config_conn.config.alternatives.find({"_id":{"$nin":self._views.keys()}}).count(True)
        self.config_conn.end_request()
        return len(self._views)+tr

    def _normalize_config(self, config):
        if "param_type" in config and hasattr(config["param_type"], "__name__"):
            config["param_type"] = config["param_type"].__name__
        if "_id" in config:
            del config["_id"]

    def update_alternative_config(self, endpoint, new_config):
        '''
        Suben la configuración de alternativa al servidor y la aplican
        localmente.

        @type endpoint: str
        @param endpoint: identificación de endpoint

        @type new_config: dict
        @param new_config: nueva configuración de alternativa
        '''
        now = time.time()
        config = self._get_alternative_config(endpoint)
        if not config:
            config = self._get_current_config(endpoint) or {}
        config.update(new_config)
        self._normalize_config(config)
        if endpoint in self._views:
            self._views[endpoint].select.config(config.copy())
            self._alternatives_skip.add(endpoint)
            # Lo ignoro en el próximo pull_alternatives
        if "probability" in config:
            # Probability se guarda como lista por limitaciones de mongodb
            config["probability"] = config["probability"].items()
        self.config_conn.config.alternatives.save(
          { "_id": endpoint,
            "config": config,
            "lt": now })
        self.config_conn.end_request()

    def _get_alternative_config(self, endpoint):
        config = self.config_conn.config.alternatives.find_one({"_id":endpoint})
        self.config_conn.end_request()
        if config:
            if "probability" in config["config"]:
                # Probability se guarda como lista por limitaciones de mongodb
                config["config"]["probability"] = dict(config["config"]["probability"])
            return config["config"]
        return None

    def _get_current_config(self, endpoint):
        if endpoint in self._views:
            return self._views[endpoint].select.current_config.copy()
        return None

    def get_alternative_config(self, endpoint):
        '''
        Devuelve la configuración de alternativa para el endpoint dado.

        @type endpoint: str
        @param endpoint: identificador de endpoint

        @rtype dict
        @type diccionario con la configuración del endpoint
        '''
        tr = self._get_alternative_config(endpoint)
        if tr is None:
            tr = self._get_current_config(endpoint)
        if tr:
            self._normalize_config(tr)
            return tr
        return {}

    def list_alternatives_methods(self):
        '''
        Lista los métodos disponibles para la selección de alternativas tal
        cual se incluirían en el campo "methods" del diccionario de
        configuración de alternativas

        @rtype str
        @return identificador del método para la selección de alternativas.
        '''
        return ManagedSelect._method_classes.keys()

    def list_alternatives_endpoints(self, endpoint):
        '''
        Lista las alternativas disponibles para el endpoint dado.

        @type endpoint: str
        @param endpoint: identificador de endpoint

        @rtype list
        @return lista de handlers alternativos para el endpoint dado
        '''
        if endpoint in self._views:
            return self._views[endpoint].alternatives.keys()
        return []

    def list_alternatives_param_types(self):
        '''
        Lista los tipos de parámetro disponibles para el método de selección
        'param' (selección por parámetro GET). Es necesario porque valor de los parámetros GET para este método es validado y parseado dependiendo de
        el tipo dado.

        @rtype list
        @return lista de tipos de parámetros disponibles para el método de
                selección del aternativas por parámetro GET.
        '''
        return ParamSelector._param_types.keys()
