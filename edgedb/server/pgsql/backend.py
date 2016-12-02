##
# Copyright (c) 2008-2013 MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##

import collections
import importlib
import json
import pickle
import re

import asyncpg

from edgedb.lang.common.algos import topological
from edgedb.lang.common.debug import debug
from edgedb.lang.common.nlang import morphology
from edgedb.lang.common import markup

from edgedb.lang.common import exceptions as edgedb_error

from edgedb.lang import schema as so
from edgedb.lang.schema import delta as sd

from edgedb.lang.schema import attributes as s_attrs
from edgedb.lang.schema import atoms as s_atoms
from edgedb.lang.schema import concepts as s_concepts
from edgedb.lang.schema import constraints as s_constr
from edgedb.lang.schema import database as s_db
from edgedb.lang.schema import ddl as s_ddl
from edgedb.lang.schema import deltarepo as s_deltarepo
from edgedb.lang.schema import deltas as s_deltas
from edgedb.lang.schema import error as s_err
from edgedb.lang.schema import expr as s_expr
from edgedb.lang.schema import functions as s_funcs
from edgedb.lang.schema import indexes as s_indexes
from edgedb.lang.schema import links as s_links
from edgedb.lang.schema import lproperties as s_lprops
from edgedb.lang.schema import modules as s_mod
from edgedb.lang.schema import name as sn
from edgedb.lang.schema import objects as s_obj
from edgedb.lang.schema import pointers as s_pointers
from edgedb.lang.schema import policy as s_policy
from edgedb.lang.schema import types as s_types

from edgedb.lang import edgeql

from edgedb.server import query as backend_query
from edgedb.server.pgsql import common
from edgedb.server.pgsql import dbops
from edgedb.server.pgsql import delta as delta_cmds
from edgedb.server.pgsql import deltadbops

from . import datasources
from .datasources import introspection

from . import astexpr
from . import compiler
from .compiler import decompiler
from . import deltarepo as pgsql_deltarepo
from . import parser
from . import schemamech
from . import types


class Cursor:
    def __init__(self, dbcursor, offset, limit):
        self.dbcursor = dbcursor
        self.offset = offset
        self.limit = limit
        self.cursor_pos = 0

    def seek(self, offset, whence='set'):
        if whence == 'set':
            if offset != self.cursor_pos:
                self.dbcursor.seek(0, 'ABSOLUTE')
                result = self.dbcursor.seek(offset, 'FORWARD')
                self.cursor_pos = result
        elif whence == 'cur':
            result = self.dbcursor.seek(offset, 'FORWARD')
            self.cursor_pos += result
        elif whence == 'end':
            result = self.dbcursor.seek('ALL')
            self.cursor_pos = result - offset

        return self.cursor_pos

    def tell(self):
        return self.cursor_pos

    def count(self, total=False):
        current = self.tell()

        if total:
            self.seek(0)
            result = self.seek(0, 'end')
        else:
            offset = self.offset if self.offset is not None else 0
            limit = self.limit if self.limit else 'ALL'

            self.seek(offset)
            result = self.seek(limit, 'cur')
            result -= offset

        self.seek(current)
        return result

    def __iter__(self):
        if self.offset:
            self.seek(self.offset)
            offset = self.offset
        else:
            offset = 0

        while self.limit is None or self.cursor_pos < offset + self.limit:
            self.cursor_pos += 1
            yield next(self.dbcursor)


class Query(backend_query.Query):
    def __init__(
            self, chunks, arg_index, argmap, result_types, argument_types,
            context_vars, scrolling_cursor=False, offset=None, limit=None,
            query_type=None, record_info=None, output_format=None):
        self.chunks = chunks
        self.text = ''.join(chunks)
        self.argmap = argmap
        self.arg_index = arg_index
        self.result_types = result_types
        self.argument_types = collections.OrderedDict((k, argument_types[k])
                                                      for k in argmap
                                                      if k in argument_types)
        self.context_vars = context_vars

        self.scrolling_cursor = scrolling_cursor
        self.offset = offset.index if offset is not None else None
        self.limit = limit.index if limit is not None else None
        self.query_type = query_type
        self.record_info = record_info
        self.output_format = output_format

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop('text')
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.text = ''.join(self.chunks)

    def get_output_format_info(self):
        if self.output_format == 'json':
            return ('json', 1)
        else:
            return ('edgedbobj', 1)

    def get_output_metadata(self):
        return {'record_info': self.record_info}


class ErrorMech:
    error_res = {
        asyncpg.IntegrityConstraintViolationError: collections.OrderedDict((
            ('link_mapping', re.compile(r'^.*".*_link_mapping_idx".*$')),
            ('link_target', re.compile(r'^.*link target constraint$')),
            ('constraint', re.compile(r'^.*;schemaconstr(?:#\d+)?".*$')),
            ('id', re.compile(r'^.*"(?:\w+)_data_pkey".*$')), ))
    }

    @classmethod
    async def _interpret_db_error(cls, backend, constr_mech, type_mech, err):
        if isinstance(err, asyncpg.NotNullViolationError):
            source_name = pointer_name = None

            if err.schema_name and err.table_name:
                tabname = (err.schema_name, err.table_name)

                source_name = backend.table_name_to_object_name(tabname)

                if err.column_name:
                    cols = await type_mech.get_table_columns(
                        tabname, connection=backend.connection)
                    col = cols.get(err.column_name)
                    pointer_name = col['column_comment']

            if pointer_name is not None:
                pname = '{{{}}}.{{{}}}'.format(source_name, pointer_name)

                return edgedb_error.MissingRequiredPointerError(
                    'missing value for required pointer {}'.format(pname),
                    source_name=source_name, pointer_name=pointer_name)

            else:
                return edgedb_error.EdgeDBBackendError(err.message)

        elif isinstance(err, asyncpg.IntegrityConstraintViolationError):
            connection = backend.connection
            schema = backend.schema
            source = pointer = None

            for ecls, eres in cls.error_res.items():
                if isinstance(err, ecls):
                    break
            else:
                eres = {}

            for type, ere in eres.items():
                m = ere.match(err.message)
                if m:
                    error_type = type
                    break
            else:
                return edgedb_error.EdgeDBBackendError(err.message)

            if error_type == 'link_mapping':
                err = 'link mapping cardinality violation'
                errcls = edgedb_error.LinkMappingCardinalityViolationError
                return errcls(err, source=source, pointer=pointer)

            elif error_type == 'link_target':
                if err.detail:
                    try:
                        detail = json.loads(err.detail)
                    except ValueError:
                        detail = None

                if detail is not None:
                    srcname = detail.get('source')
                    ptrname = detail.get('pointer')
                    target = detail.get('target')
                    expected = detail.get('expected')

                    if srcname and ptrname:
                        srcname = sn.Name(srcname)
                        ptrname = sn.Name(ptrname)
                        lname = '{}.{}'.format(srcname, ptrname.name)
                    else:
                        lname = ''

                    msg = 'invalid target for link {!r}: {!r} (' \
                          'expecting {!r})'.format(lname, target,
                                                   ' or '.join(expected))

                else:
                    msg = 'invalid target for link'

                return edgedb_error.InvalidPointerTargetError(msg)

            elif error_type == 'constraint':
                constraint_name = \
                    await constr_mech.constraint_name_from_pg_name(
                        connection, err.constraint_name)

                if constraint_name is None:
                    return edgedb_error.EdgeDBBackendError(err.message)

                constraint = schema.get(constraint_name)

                return edgedb_error.ConstraintViolationError(
                    constraint.format_error_message())

            elif error_type == 'id':
                msg = 'unique link constraint violation'
                errcls = edgedb_error.UniqueConstraintViolationError
                return errcls(msg=msg)
        else:
            return edgedb_error.EdgeDBBackendError(err.message)


class Backend(s_deltarepo.DeltaProvider):

    typlen_re = re.compile(
        r"""
        (?P<type>.*) \( (?P<length>\d+ (?:\s*,\s*(\d+))*) \)$
    """, re.X)

    search_idx_name_re = re.compile(
        r"""
        .*_(?P<language>\w+)_(?P<index_class>\w+)_search_idx$
    """, re.X)

    link_source_colname = common.quote_ident(
        common.edgedb_name_to_pg_name('std::source'))
    link_target_colname = common.quote_ident(
        common.edgedb_name_to_pg_name('std::target'))

    def __init__(self, connection):
        self.modules = None

        self.schema = None

        self._constr_mech = schemamech.ConstraintMech()
        self._type_mech = schemamech.TypeMech()

        self.atom_cache = {}
        self.link_cache = {}
        self.link_property_cache = {}
        self.concept_cache = {}
        self.table_cache = {}
        self.domain_to_atom_map = {}
        self.table_id_to_class_name_cache = {}
        self.classname_to_table_id_cache = {}
        self.attribute_link_map_cache = {}
        self._record_mapping_cache = {}

        self.parser = parser.PgSQLParser()
        self.search_idx_expr = astexpr.TextSearchExpr()
        self.type_expr = astexpr.TypeExpr()
        self.constant_expr = None

        self.connection = connection

        repo = pgsql_deltarepo.MetaDeltaRepository(self.connection)
        super().__init__(repo)

    def get_constr_mech(self):
        return self._constr_mech

    async def _init_introspection_cache(self):
        await self._type_mech.init_cache(self.connection)
        await self._constr_mech.init_cache(self.connection)
        t2pn, pn2t = await self._init_relid_cache()
        self.table_id_to_class_name_cache = t2pn
        self.classname_to_table_id_cache = pn2t
        self.domain_to_atom_map = await self._init_atom_map_cache()
        # Concept map needed early for type filtering operations
        # in schema queries
        await self.get_concept_map(force_reload=True)

    async def _init_relid_cache(self):
        ds = introspection.tables.TableList(self.connection)
        link_tables = await ds.fetch(
            schema_name='edgedb%', table_pattern='%_link')
        link_tables = {(t['schema'], t['name']): t for t in link_tables}

        ds = introspection.types.TypesList(self.connection)
        records = await ds.fetch(
            schema_name='edgedb%', type_name='%_record', include_arrays=False)
        records = {(t['schema'], t['name']): t for t in records}

        ds = datasources.schema.links.ConceptLinks(self.connection)
        links_list = await ds.fetch()
        links_list = collections.OrderedDict((sn.Name(r['name']), r)
                                             for r in links_list)

        table_id_to_class_name_cache = {}
        classname_to_table_id_cache = {}

        for link_name, link in links_list.items():
            link_table_name = common.link_name_to_table_name(
                link_name, catenate=False)
            t = link_tables.get(link_table_name)
            if t:
                table_id_to_class_name_cache[t['oid']] = link_name
                table_id_to_class_name_cache[t['typoid']] = link_name
                classname_to_table_id_cache[link_name] = t['typoid']

        ds = introspection.tables.TableList(self.connection)
        tables = await ds.fetch(schema_name='edgedb%', table_pattern='%_data')
        tables = {(t['schema'], t['name']): t for t in tables}

        ds = datasources.schema.concepts.ConceptList(self.connection)
        concept_list = await ds.fetch()
        concept_list = collections.OrderedDict((sn.Name(row['name']), row)
                                               for row in concept_list)

        for name, row in concept_list.items():
            table_name = common.concept_name_to_table_name(
                name, catenate=False)
            table = tables.get(table_name)

            if not table:
                msg = 'internal metadata incosistency'
                details = 'Record for concept {!r} exists but the ' \
                          'table is missing'.format(name)
                raise s_err.SchemaError(msg, details=details)

            table_id_to_class_name_cache[table['oid']] = name
            table_id_to_class_name_cache[table['typoid']] = name
            classname_to_table_id_cache[name] = table['typoid']

        return table_id_to_class_name_cache, classname_to_table_id_cache

    def table_name_to_object_name(self, table_name):
        return self.table_cache.get(table_name)['name']

    async def _init_atom_map_cache(self):
        ds = introspection.domains.DomainsList(self.connection)
        domains = await ds.fetch(schema_name='edgedb%', domain_name='%_domain')
        domains = {(d['schema'], d['name']): self.normalize_domain_descr(d)
                   for d in domains}

        ds = datasources.schema.atoms.AtomList(self.connection)
        atom_list = await ds.fetch()

        domain_to_atom_map = {}

        for row in atom_list:
            name = sn.Name(row['name'])

            domain_name = common.atom_name_to_domain_name(name, catenate=False)
            domain_to_atom_map[domain_name] = name

        return domain_to_atom_map

    async def readschema(self):
        schema = so.Schema()
        await self._init_introspection_cache()
        await self.read_modules(schema)
        await self.read_atoms(schema)
        await self.read_attributes(schema)
        await self.read_actions(schema)
        await self.read_events(schema)
        await self.read_functions(schema)
        await self.read_concepts(schema)
        await self.read_links(schema)
        await self.read_link_properties(schema)
        await self.read_policies(schema)
        await self.read_attribute_values(schema)
        await self.read_constraints(schema)

        await self.order_attributes(schema)
        await self.order_actions(schema)
        await self.order_events(schema)
        await self.order_atoms(schema)
        await self.order_functions(schema)
        await self.order_link_properties(schema)
        await self.order_links(schema)
        await self.order_concepts(schema)
        await self.order_policies(schema)

        return schema

    async def getschema(self):
        if self.schema is None:
            self.schema = await self.readschema()

        return self.schema

    def adapt_delta(self, delta):
        return delta_cmds.CommandMeta.adapt(delta)

    @debug
    def process_delta(self, delta, schema, session=None):
        """Adapt and process the delta command."""
        """LOG [delta.plan] Delta Plan
            markup.dump(delta)
        """
        delta = self.adapt_delta(delta)
        connection = session.get_connection() if session else self.connection
        context = delta_cmds.CommandContext(connection, session=session)
        delta.apply(schema, context)
        """LOG [delta.plan.pgsql] PgSQL Delta Plan
            markup.dump(delta)
        """
        return delta

    @debug
    async def run_delta_command(self, delta_cmd):
        schema = await self.getschema()
        context = sd.CommandContext()
        result = None

        with context(s_deltas.DeltaCommandContext(delta_cmd)):
            delta = delta_cmd.apply(schema, context)

            if isinstance(delta_cmd, s_deltas.CommitDelta):
                ddl_plan = s_db.AlterDatabase()

                if delta.commands:
                    ddl_plan.update(delta.commands)
                    schema0 = schema.copy()
                else:
                    schema0 = schema

                if delta.target is not None:
                    diff = sd.delta_schemas(delta.target, schema0)
                    """LOG [migration.ddl] Migration DDL
                        markup.dump(diff)
                    """
                    ddl_plan.update(diff)

                await self.run_ddl_command(ddl_plan)
                await self._commit_delta(delta, ddl_plan)

            elif isinstance(delta_cmd, s_deltas.CreateDelta):
                pass

            elif isinstance(delta_cmd, s_deltas.GetDelta):
                result = s_ddl.ddl_text_from_delta(schema, delta)

            else:
                raise RuntimeError(
                    f'unexpected delta command: {delta_cmd!r}')

        return result

    async def _commit_delta(self, delta, ddl_plan):
        return  # XXX
        table = deltadbops.DeltaTable()
        rec = table.record(
            name=delta.name, module_id=dbops.Query(
                '''
                SELECT id FROM edgedb.module WHERE name = $1
            ''', params=[delta.name.module]), parents=dbops.Query(
                    '''
                SELECT array_agg(id) FROM edgedb.delta WHERE name = any($1)
            ''', params=[[parent.name for parent in delta.parents]]),
            checksum=(await self.getschema()).get_checksum(), deltabin=b'1',
            deltasrc=s_ddl.ddl_text_from_delta_command(ddl_plan))
        context = delta_cmds.CommandContext(self.connection, None)
        await dbops.Insert(table, records=[rec]).execute(context)

    @debug
    async def run_ddl_command(self, ddl_plan):
        schema = await self.getschema()
        """LOG [delta.plan.input] Delta Plan Input
            markup.dump(ddl_plan)
        """

        test_schema = await self.readschema()
        context = sd.CommandContext()
        canonical_ddl_plan = ddl_plan.copy()
        canonical_ddl_plan.apply(test_schema, context=context)

        # Apply and adapt delta, build native delta plan
        plan = self.process_delta(canonical_ddl_plan, schema)

        context = delta_cmds.CommandContext(self.connection, None)

        try:
            if not isinstance(plan, (s_db.CreateDatabase, s_db.DropDatabase)):
                async with self.connection.transaction():
                    await plan.execute(context)
            else:
                await plan.execute(context)
        except Exception as e:
            await self.getschema()
            msg = 'failed to apply delta to data backend'
            raise RuntimeError(msg) from e

        await self.invalidate_schema_cache()
        await self.getschema()

    async def invalidate_schema_cache(self):
        self.schema = None
        self.invalidate_transient_cache()

    def invalidate_transient_cache(self):
        self._constr_mech.invalidate_schema_cache()
        self._type_mech.invalidate_schema_cache()

        self.link_cache.clear()
        self.link_property_cache.clear()
        self.concept_cache.clear()
        self.atom_cache.clear()
        self.table_cache.clear()
        self.domain_to_atom_map.clear()
        self.table_id_to_class_name_cache.clear()
        self.classname_to_table_id_cache.clear()
        self.attribute_link_map_cache.clear()

    async def get_concept_map(self, force_reload=False):
        connection = self.connection

        if not self.concept_cache or force_reload:
            cl_ds = datasources.schema.concepts.ConceptList(connection)

            for row in await cl_ds.fetch():
                self.concept_cache[row['name']] = row['id']
                self.concept_cache[row['id']] = sn.Name(row['name'])

        return self.concept_cache

    def get_concept_id(self, concept):
        concept_id = None

        concept_cache = self.concept_cache
        if concept_cache:
            concept_id = concept_cache.get(concept.name)

        if concept_id is None:
            msg = 'could not determine backend id for concept in this context'
            details = 'Concept: {}'.format(concept.name)
            raise s_err.SchemaError(msg, details=details)

        return concept_id

    def source_name_from_relid(self, table_oid):
        return self.table_id_to_class_name_cache.get(table_oid)

    def typrelid_for_source_name(self, source_name):
        return self.classname_to_table_id_cache.get(source_name)

    def compile(self, query_ir, scrolling_cursor=False, context=None, *,
                output_format=None):
        if scrolling_cursor:
            offset = query_ir.offset
            limit = query_ir.limit
        else:
            offset = limit = None

        if scrolling_cursor:
            query_ir.offset = None
            query_ir.limit = None

        ir_compiler = compiler.IRCompiler()

        qchunks, argmap, arg_index, query_type, record_info = \
            ir_compiler.transform(query_ir, backend=self, schema=self.schema,
                                  output_format=output_format)

        if scrolling_cursor:
            query_ir.offset = offset
            query_ir.limit = limit

        restypes = {'_': query_ir.result_types}
        argtypes = {}

        for k, v in query_ir.argument_types.items():
            argtypes[k] = v

        return Query(
            chunks=qchunks, arg_index=arg_index, argmap=argmap,
            result_types=restypes, argument_types=argtypes,
            context_vars=query_ir.context_vars,
            scrolling_cursor=scrolling_cursor, offset=offset, limit=limit,
            query_type=query_type, record_info=record_info,
            output_format=output_format)

    async def read_modules(self, schema):
        ds = introspection.schemas.SchemasList(self.connection)
        schemas = await ds.fetch(schema_name='edgedb_%')
        schemas = {
            s['name']
            for s in schemas if not s['name'].startswith('edgedb_aux_')
        }

        ds = datasources.schema.modules.ModuleList(self.connection)
        modules = await ds.fetch()
        modules = {
            common.edgedb_module_name_to_schema_name(m['name']):
            {'name': m['name'],
             'imports': m['imports']}
            for m in modules
        }

        recorded_schemas = set(modules.keys())

        # Sanity checks
        extra_schemas = schemas - recorded_schemas - {'edgedb', 'edgedbss'}
        missing_schemas = recorded_schemas - schemas

        if extra_schemas:
            msg = 'internal metadata incosistency'
            details = 'Extraneous data schemas exist: {}'.format(
                ', '.join('"%s"' % s for s in extra_schemas))
            raise s_err.SchemaError(msg, details=details)

        if missing_schemas:
            msg = 'internal metadata incosistency'
            details = 'Missing schemas for modules: {}'.format(
                ', '.join('{!r}'.format(s) for s in missing_schemas))
            raise s_err.SchemaError(msg, details=details)

        mods = []

        for module in modules.values():
            mod = s_mod.Module(
                name=module['name'])
            schema.add_module(mod)
            mods.append(mod)

        for mod in mods:
            for imp_name in mod.imports:
                if not schema.has_module(imp_name):
                    # Must be a foreign module, import it directly
                    try:
                        impmod = importlib.import_module(imp_name)
                    except ImportError:
                        # Module has moved, create a dummy
                        impmod = so.DummyModule(imp_name)

                    schema.add_module(impmod)

    async def read_atoms(self, schema):
        ds = introspection.domains.DomainsList(self.connection)
        domains = await ds.fetch(schema_name='edgedb%', domain_name='%_domain')
        domains = {(d['schema'], d['name']): self.normalize_domain_descr(d)
                   for d in domains}

        ds = introspection.sequences.SequencesList(self.connection)
        seqs = await ds.fetch(
            schema_name='edgedb%', sequence_pattern='%_sequence')
        seqs = {(s['schema'], s['name']): s for s in seqs}

        seen_seqs = set()

        ds = datasources.schema.atoms.AtomList(self.connection)
        atom_list = await ds.fetch()

        basemap = {}

        for row in atom_list:
            name = sn.Name(row['name'])

            atom_data = {
                'name': name,
                'title': self.json_to_word_combination(row['title']),
                'description': row['description'],
                'is_abstract': row['is_abstract'],
                'is_final': row['is_final'],
                'bases': row['bases'],
                'default': row['default'],
            }

            self.atom_cache[name] = atom_data
            atom_data['default'] = self.unpack_default(row['default'])

            if atom_data['bases']:
                basemap[name] = atom_data['bases']

            atom = s_atoms.Atom(
                name=name, default=atom_data['default'],
                title=atom_data['title'], description=atom_data['description'],
                is_abstract=atom_data['is_abstract'],
                is_final=atom_data['is_final'])

            schema.add(atom)

        for atom in schema.get_objects(type='atom'):
            try:
                basename = basemap[atom.name]
            except KeyError:
                pass
            else:
                atom.bases = [schema.get(sn.Name(basename[0]))]

        sequence = schema.get('std::sequence', None)
        for atom in schema.get_objects(type='atom'):
            if sequence is not None and atom.issubclass(sequence):
                seq_name = common.atom_name_to_sequence_name(
                    atom.name, catenate=False)
                if seq_name not in seqs:
                    msg = 'internal metadata incosistency'
                    details = 'Missing sequence for sequence atom {!r}'.format(
                        atom.name)
                    raise s_err.SchemaError(msg, details=details)
                seen_seqs.add(seq_name)

        extra_seqs = set(seqs) - seen_seqs
        if extra_seqs:
            msg = 'internal metadata incosistency'
            details = 'Extraneous sequences exist: {}'.format(
                ', '.join(common.qname(*t) for t in extra_seqs))
            raise s_err.SchemaError(msg, details=details)

    async def order_atoms(self, schema):
        for atom in schema.get_objects(type='atom'):
            atom.acquire_ancestor_inheritance(schema)

    async def read_functions(self, schema):
        ds = datasources.schema.functions.FunctionList(self.connection)
        func_list = await ds.fetch()

        for row in func_list:
            name = sn.Name(row['name'])

            func_data = {
                'name': name,
                'title': self.json_to_word_combination(row['title']),
                'description': row['description'],
                'is_abstract': row['is_abstract'],
                'is_final': row['is_final'],
                'aggregate': row['aggregate'],
                'paramtypes': so.ClassDict({
                    k: schema.get(v)
                    for k, v in json.loads(row['paramtypes'])
                }) if row['paramtypes'] else None,
                'paramkinds':
                (json.loads(row['paramkinds']) if row['paramkinds'] else None),
                'paramdefaults': (
                    json.loads(row['paramdefaults'])
                    if row['paramdefaults'] else None),
                'returntype': schema.get(row['returntype'])
            }

            func = s_funcs.Function(**func_data)
            schema.add(func)

    async def order_functions(self, schema):
        pass

    async def read_constraints(self, schema):
        ds = datasources.schema.constraints.Constraints(self.connection)
        constraints_list = await ds.fetch()
        constraints_list = collections.OrderedDict((sn.Name(r['name']), r)
                                                   for r in constraints_list)

        basemap = {}

        for name, r in constraints_list.items():
            bases = tuple()

            if r['subject']:
                bases = (s_constr.Constraint.get_shortname(name), )
            elif r['bases']:
                bases = tuple(sn.Name(b) for b in r['bases'])
            elif name != 'std::constraint':
                bases = (sn.Name('std::constraint'), )

            title = self.json_to_word_combination(r['title'])
            description = r['description']
            subject = schema.get(r['subject']) if r['subject'] else None

            basemap[name] = bases

            allparamtypes = {}

            if r['inferredparamtypes']:
                inferredparamtypes = {
                    n: self.unpack_typeref(v, schema)
                    for n, v in json.loads(r['inferredparamtypes']).items()
                }
                allparamtypes.update(inferredparamtypes)
            else:
                inferredparamtypes = None

            if r['paramtypes']:
                paramtypes = {
                    n: self.unpack_typeref(v, schema)
                    for n, v in json.loads(r['paramtypes']).items()
                }
                allparamtypes.update(paramtypes)
            else:
                paramtypes = None

            if r['args']:
                args = json.loads(r['args'])
                for k, v in args.items():
                    paramtype = allparamtypes[k]
                    args[k] = paramtype.coerce(v, schema)
            else:
                args = None

            constraint = s_constr.Constraint(
                name=name, subject=subject, title=title,
                description=description, is_abstract=r['is_abstract'],
                is_final=r['is_final'], expr=r['expr'],
                subjectexpr=r['subjectexpr'],
                localfinalexpr=r['localfinalexpr'], finalexpr=r['finalexpr'],
                errmessage=r['errmessage'], paramtypes=paramtypes,
                inferredparamtypes=inferredparamtypes, args=args)

            if subject:
                subject.add_constraint(constraint)

            schema.add(constraint)

        for constraint in schema.get_objects(type='constraint'):
            try:
                bases = basemap[constraint.name]
            except KeyError:
                pass
            else:
                constraint.bases = [schema.get(b) for b in bases]

        for constraint in schema.get_objects(type='constraint'):
            constraint.acquire_ancestor_inheritance(schema)

    async def order_constraints(self, schema):
        pass

    def unpack_typeref(self, typeref, schema):
        if typeref['type'] is not None:
            type = schema.get(typeref['type'])

        if typeref['collection'] is not None:
            coll_type = s_obj.Collection.get_class(typeref['collection'])
            subtypes = [schema.get(st) for st in typeref['subtypes']]
            type = coll_type.from_subtypes(subtypes)

        return type

    def unpack_default(self, value):
        result = None
        if value is not None:
            val = json.loads(value)
            if val['type'] == 'expr':
                result = s_expr.ExpressionText(val['value'])
            else:
                result = val['value']
        return result

    def interpret_search_index(self, index):
        m = self.search_idx_name_re.match(index.name)
        if not m:
            msg = 'could not interpret index {}'.format(index.name)
            raise s_err.SchemaError(msg)

        language = m.group('language')
        index_class = m.group('index_class')

        tree = self.parser.parse(index.expr)
        columns = self.search_idx_expr.match(tree)

        if columns is None:
            msg = 'could not interpret index {!r}'.format(str(index.name))
            details = 'Could not match expression:\n{}'.format(
                markup.dumps(tree))
            hint = 'Take a look at the matching pattern and adjust'
            raise s_err.SchemaError(msg, details=details, hint=hint)

        return index_class, language, columns

    def interpret_search_indexes(self, table_name, indexes):
        for idx_data in indexes:
            index = dbops.Index.from_introspection(table_name, idx_data)
            yield self.interpret_search_index(index)

    async def read_search_indexes(self):
        indexes = {}
        index_ds = datasources.introspection.tables.TableIndexes(
            self.connection)
        idx_data = await index_ds.fetch(
            schema_pattern='edgedb%', index_pattern='%_search_idx')

        for row in idx_data:
            table_name = tuple(row['table_name'])
            tabidx = indexes[table_name] = {}

            si = self.interpret_search_indexes(table_name, row['indexes'])

            for index_class, language, columns in si:
                for column_name, column_config in columns.items():
                    idx = tabidx.setdefault(column_name, {})
                    idx[(index_class, column_config[0])] = \
                        s_links.LinkSearchWeight(column_config[1])

        return indexes

    def interpret_index(self, index):
        index_expression = index.expr

        if not index_expression:
            index_expression = '(%s)' % ', '.join(
                common.quote_ident(c) for c in index.columns)

        return self.parser.parse(index_expression)

    def interpret_indexes(self, table_name, indexes):
        for idx_data in indexes:
            idx = dbops.Index.from_introspection(table_name, idx_data)
            yield idx, self.interpret_index(idx)

    async def read_indexes(self):
        indexes = {}
        index_ds = datasources.introspection.tables.TableIndexes(
            self.connection)
        idx_data = await index_ds.fetch(
            schema_pattern='edgedb%', index_pattern='%_reg_idx')

        for row in idx_data:
            table_name = tuple(row['table_name'])
            indexes[table_name] = set(
                self.interpret_indexes(table_name, row['indexes']))

        return indexes

    def interpret_sql(self, expr, source=None):
        try:
            expr_tree = self.parser.parse(expr)
        except parser.PgSQLParserError as e:
            msg = 'could not interpret constant expression "%s"' % expr
            details = 'Syntax error when parsing expression: %s' % e.args[0]
            raise s_err.SchemaError(msg, details=details) from e

        if not self.constant_expr:
            self.constant_expr = astexpr.ConstantExpr()

        result = self.constant_expr.match(expr_tree)

        if result is None:
            sql_decompiler = decompiler.Decompiler()
            edgedb_tree = sql_decompiler.transform(expr_tree, source)
            edgeql_tree = edgeql.decompile_ir(
                edgedb_tree, return_statement=True)
            result = edgeql.generate_source(edgeql_tree, pretty=False)
            result = s_expr.ExpressionText(result)

        return result

    async def read_pointer_target_column(self, schema, pointer,
                                         constraints_cache):
        ptr_stor_info = types.get_pointer_storage_info(
            pointer, schema=schema, resolve_type=False)
        cols = await self._type_mech.get_table_columns(
            ptr_stor_info.table_name, connection=self.connection)

        col = cols.get(ptr_stor_info.column_name)

        if not col:
            msg = 'internal metadata inconsistency'
            details = (
                'Record for {!r} hosted by {!r} exists, but ' +
                'the corresponding table column is missing').format(
                    pointer.shortname, pointer.source.name)
            raise s_err.SchemaError(msg, details=details)

        return self._get_pointer_column_target(
            schema, pointer.source, pointer.shortname, col)

    def _get_pointer_column_target(self, schema, source, pointer_name, col):
        if col['column_type_schema'] == 'pg_catalog':
            col_type_schema = common.edgedb_module_name_to_schema_name('std')
            col_type = col['column_type_formatted']
        else:
            col_type_schema = col['column_type_schema']
            col_type = col['column_type_formatted'] or col['column_type']

        if col['column_default'] is not None:
            atom_default = self.interpret_sql(col['column_default'], source)
        else:
            atom_default = None

        target = self.atom_from_pg_type(
            col_type, col_type_schema, atom_default, schema)

        return target, col['column_required']

    def _get_pointer_attribute_target(
            self, schema, source, pointer_name, attr):
        if attr['attribute_type_schema'] == 'pg_catalog':
            col_type_schema = common.edgedb_module_name_to_schema_name('std')
            col_type = attr['attribute_type_formatted']
        else:
            col_type_schema = attr['attribute_type_schema']
            col_type = \
                attr['attribute_type_formatted'] or attr['attribute_type']

        if attr['attribute_default'] is not None:
            atom_default = self.interpret_sql(
                attr['attribute_default'], source)
        else:
            atom_default = None

        if attr['attribute_type_composite_id']:
            # composite record
            source_name = self.source_name_from_relid(
                attr['attribute_type_composite_id'])
            target = schema.get(source_name)
        else:
            target = self.atom_from_pg_type(
                col_type, col_type_schema, atom_default, schema)

        return target, attr['attribute_required']

    def verify_ptr_const_defaults(self, schema, ptr, tab_default):
        return
        schema_default = None

        if ptr.default is not None:
            if isinstance(ptr.default, s_expr.ExpressionText):
                default_value = schemamech.ptr_default_to_col_default(
                    schema, ptr, ptr.default)
                if default_value is not None:
                    schema_default = ptr.default
            else:
                schema_default = ptr.default

        if tab_default is None:
            if schema_default:
                msg = 'internal metadata inconsistency'
                details = (
                    'Literal default for pointer {!r} is present in ' +
                    'the schema, but not in the table').format(ptr.name)
                raise s_err.SchemaError(msg, details=details)
            else:
                return

        table_default = self.interpret_sql(tab_default, ptr.source)

        if tab_default is not None and not ptr.default:
            msg = 'internal metadata inconsistency'
            details = (
                'Literal default for pointer {!r} is present in ' +
                'the table, but not in schema declaration').format(ptr.name)
            raise s_err.SchemaError(msg, details=details)

        if not isinstance(table_default, s_expr.ExpressionText):
            typ = ptr.target.get_topmost_base()
            typ_t = s_types.BaseTypeMeta.get_implementation(typ.name)
            assert typ_t, 'missing implementation for {}'.format(typ.name)
            table_default = typ_t(table_default)
            schema_default = typ_t(schema_default)

        if schema_default != table_default:
            msg = 'internal metadata inconsistency'
            details = (
                'Value mismatch in literal default pointer link ' +
                '{!r}: {!r} in the table vs. {!r} in the schema').format(
                    ptr.name, table_default, schema_default)
            raise s_err.SchemaError(msg, details=details)

    async def read_links(self, schema):
        ds = introspection.tables.TableList(self.connection)
        link_tables = await ds.fetch(
            schema_name='edgedb%', table_pattern='%_link')
        link_tables = {(t['schema'], t['name']): t for t in link_tables}

        ds = datasources.schema.links.ConceptLinks(self.connection)
        links_list = await ds.fetch()
        links_list = collections.OrderedDict((sn.Name(r['name']), r)
                                             for r in links_list)

        concept_indexes = await self.read_search_indexes()
        basemap = {}

        for name, r in links_list.items():
            bases = tuple()

            if r['source']:
                bases = (s_links.Link.get_shortname(name), )
            elif r['bases']:
                bases = tuple(sn.Name(b) for b in r['bases'])
            elif name != 'std::link':
                bases = (sn.Name('std::link'), )

            title = self.json_to_word_combination(r['title'])
            description = r['description']

            source = schema.get(r['source']) if r['source'] else None
            target = schema.get(r['target']) if r['target'] else None
            if r['spectargets']:
                spectargets = [schema.get(t) for t in r['spectargets']]
            else:
                spectargets = None

            default = self.unpack_default(r['default'])

            required = r['required']

            if r['loading']:
                loading = s_pointers.PointerLoading(r['loading'])
            else:
                loading = None

            if r['exposed_behaviour']:
                exposed_behaviour = \
                    s_pointers.PointerExposedBehaviour(r['exposed_behaviour'])
            else:
                exposed_behaviour = None

            if r['mapping']:
                mapping = s_links.LinkMapping(r['mapping'])
            else:
                mapping = None

            basemap[name] = bases

            link = s_links.Link(
                name=name, source=source, target=target,
                spectargets=spectargets, mapping=mapping,
                exposed_behaviour=exposed_behaviour, required=required,
                title=title, description=description,
                is_abstract=r['is_abstract'], is_final=r['is_final'],
                readonly=r['readonly'], loading=loading, default=default)

            if spectargets:
                # Multiple specified targets,
                # target is a virtual derived object
                target = link.create_common_target(schema, spectargets)

            link_search = None

            if isinstance(target, s_atoms.Atom):
                target, required = await self.read_pointer_target_column(
                    schema, link, None)

                concept_schema, concept_table = \
                    common.concept_name_to_table_name(source.name,
                                                      catenate=False)

                indexes = concept_indexes.get((concept_schema, concept_table))

                if indexes:
                    col_search_index = indexes.get(bases[0])
                    if col_search_index:
                        weight = col_search_index[('default', 'english')]
                        link_search = s_links.LinkSearchConfiguration(
                            weight=weight)

            link.target = target

            if link_search:
                link.search = link_search

            if source:
                source.add_pointer(link)

            schema.add(link)

        for link in schema.get_objects(type='link'):
            try:
                bases = basemap[link.name]
            except KeyError:
                pass
            else:
                link.bases = [schema.get(b) for b in bases]

        for link in schema.get_objects(type='link'):
            link.acquire_ancestor_inheritance(schema)

    async def order_links(self, schema):
        indexes = await self.read_indexes()

        sql_decompiler = decompiler.Decompiler()

        g = {}

        for link in schema.get_objects(type='link'):
            g[link.name] = {"item": link, "merge": [], "deps": []}
            if link.bases:
                g[link.name]['merge'].extend(b.name for b in link.bases)

        topological.normalize(g, merger=s_links.Link.merge, schema=schema)

        for link in schema.get_objects(type='link'):
            link.finalize(schema)

        for link in schema.get_objects(type='link'):
            if link.generic():
                table_name = common.get_table_name(link, catenate=False)
                tabidx = indexes.get(table_name)
                if tabidx:
                    for index, index_sql in tabidx:
                        if index.get_metadata('ddl:inherited'):
                            continue

                        edgedb_tree = sql_decompiler.transform(index_sql, link)
                        edgeql_tree = edgeql.decompile_ir(
                            edgedb_tree, return_statement=True)
                        expr = edgeql.generate_source(
                            edgeql_tree, pretty=False)
                        schema_name = index.get_metadata('schemaname')
                        index = s_indexes.SourceIndex(
                            name=sn.Name(schema_name), subject=link, expr=expr)
                        link.add_index(index)
                        schema.add(index)
            elif link.atomic():
                ptr_stor_info = types.get_pointer_storage_info(
                    link, schema=schema)
                cols = await self._type_mech.get_table_columns(
                    ptr_stor_info.table_name, connection=self.connection)
                col = cols[ptr_stor_info.column_name]
                self.verify_ptr_const_defaults(
                    schema, link, col['column_default'])

    async def read_link_properties(self, schema):
        ds = datasources.schema.links.LinkProperties(self.connection)
        link_props = await ds.fetch()
        link_props = collections.OrderedDict((sn.Name(r['name']), r)
                                             for r in link_props)
        basemap = {}

        for name, r in link_props.items():
            bases = ()

            if r['source']:
                bases = (s_lprops.LinkProperty.get_shortname(name), )
            elif r['bases']:
                bases = tuple(sn.Name(b) for b in r['bases'])
            elif name != 'std::linkproperty':
                bases = (sn.Name('std::linkproperty'), )

            title = self.json_to_word_combination(r['title'])
            description = r['description']
            source = schema.get(r['source']) if r['source'] else None

            default = self.unpack_default(r['default'])

            required = r['required']
            target = None

            if r['loading']:
                loading = s_pointers.PointerLoading(r['loading'])
            else:
                loading = None

            basemap[name] = bases

            prop = s_lprops.LinkProperty(
                name=name, source=source, target=target, required=required,
                title=title, description=description, readonly=r['readonly'],
                loading=loading, default=default)

            if source and bases[0] not in {'std::target', 'std::source'}:
                # The property is attached to a link, check out
                # link table columns for target information.
                target, required = \
                    await self.read_pointer_target_column(schema, prop, None)
            else:
                if bases:
                    if bases[0] == 'std::target' and source is not None:
                        target = source.target
                    elif bases[0] == 'std::source' and source is not None:
                        target = source.source

            prop.target = target

            if source:
                prop.acquire_ancestor_inheritance(schema)
                source.add_pointer(prop)

            schema.add(prop)

        for prop in schema.get_objects(type='link_property'):
            try:
                bases = basemap[prop.name]
            except KeyError:
                pass
            else:
                prop.bases = [
                    schema.get(b, type=s_lprops.LinkProperty) for b in bases
                ]

    async def order_link_properties(self, schema):
        g = {}

        for prop in schema.get_objects(type='link_property'):
            g[prop.name] = {"item": prop, "merge": [], "deps": []}
            if prop.bases:
                g[prop.name]['merge'].extend(b.name for b in prop.bases)

        topological.normalize(
            g, merger=s_lprops.LinkProperty.merge, schema=schema)

        for prop in schema.get_objects(type='link_property'):
            prop.finalize(schema)

            if not prop.generic() and prop.source.generic():
                source_table_name = common.get_table_name(
                    prop.source, catenate=False)
                cols = await self._type_mech.get_table_columns(
                    source_table_name, connection=self.connection)
                col_name = common.edgedb_name_to_pg_name(prop.shortname)
                col = cols[col_name]
                self.verify_ptr_const_defaults(
                    schema, prop, col['column_default'])

    async def read_attributes(self, schema):
        attributes_ds = datasources.schema.attributes.Attributes(
            self.connection)
        attributes = await attributes_ds.fetch()

        for r in attributes:
            name = sn.Name(r['name'])
            title = self.json_to_word_combination(r['title'])
            description = r['description']

            coll = r['type']['collection']
            if coll:
                stypes = [schema.get(st) for st in r['type']['subtypes']]
                ct = s_obj.Collection.get_class(coll)
                type = ct.from_subtypes(stypes)
            else:
                type = schema.get(r['type']['type'])

            attribute = s_attrs.Attribute(
                name=name, title=title, description=description, type=type)
            schema.add(attribute)

    async def order_attributes(self, schema):
        pass

    async def read_attribute_values(self, schema):
        attributes_ds = datasources.schema.attributes.AttributeValues(
            self.connection)
        attributes = await attributes_ds.fetch()

        for r in attributes:
            name = sn.Name(r['name'])
            subject = schema.get(r['subject_name'])
            attribute = schema.get(r['attribute_name'])
            value = pickle.loads(r['value'])

            attribute = s_attrs.AttributeValue(
                name=name, subject=subject, attribute=attribute, value=value)
            subject.add_attribute(attribute)
            schema.add(attribute)

    async def read_actions(self, schema):
        actions_ds = datasources.schema.policy.Actions(self.connection)
        actions = await actions_ds.fetch()

        for r in actions:
            name = sn.Name(r['name'])
            title = self.json_to_word_combination(r['title'])
            description = r['description']

            action = s_policy.Action(
                name=name, title=title, description=description)
            schema.add(action)

    async def order_actions(self, schema):
        pass

    async def read_events(self, schema):
        events_ds = datasources.schema.policy.Events(self.connection)
        events = await events_ds.fetch()

        basemap = {}

        for r in events:
            name = sn.Name(r['name'])
            title = self.json_to_word_combination(r['title'])
            description = r['description']

            if r['bases']:
                bases = tuple(sn.Name(b) for b in r['bases'])
            elif name != 'std::event':
                bases = (sn.Name('std::event'), )
            else:
                bases = tuple()

            basemap[name] = bases

            event = s_policy.Event(
                name=name, title=title, description=description)
            schema.add(event)

        for event in schema.get_objects(type='event'):
            try:
                bases = basemap[event.name]
            except KeyError:
                pass
            else:
                event.bases = [schema.get(b) for b in bases]

        for event in schema.get_objects(type='event'):
            event.acquire_ancestor_inheritance(schema)

    async def order_events(self, schema):
        pass

    async def read_policies(self, schema):
        policies_ds = datasources.schema.policy.Policies(self.connection)
        policies = await policies_ds.fetch()

        for r in policies:
            name = sn.Name(r['name'])
            title = self.json_to_word_combination(r['title'])
            description = r['description']
            policy = s_policy.Policy(
                name=name, title=title, description=description,
                subject=schema.get(r['subject']), event=schema.get(r['event']),
                actions=[schema.get(a) for a in r['actions']])
            schema.add(policy)
            policy.subject.add_policy(policy)

    async def order_policies(self, schema):
        pass

    async def get_type_attributes(self, type_name, connection=None,
                                  cache='auto'):
        return await self._type_mech.get_type_attributes(
            type_name, connection, cache)

    async def read_concepts(self, schema):
        ds = introspection.tables.TableList(self.connection)
        tables = await ds.fetch(schema_name='edgedb%', table_pattern='%_data')
        tables = {(t['schema'], t['name']): t for t in tables}

        ds = datasources.schema.concepts.ConceptList(self.connection)
        concept_list = await ds.fetch()
        concept_list = collections.OrderedDict((sn.Name(row['name']), row)
                                               for row in concept_list)

        visited_tables = set()

        self.table_cache.update({
            common.concept_name_to_table_name(n, catenate=False): c
            for n, c in concept_list.items()
        })

        basemap = {}

        for name, row in concept_list.items():
            concept = {
                'name': name,
                'title': self.json_to_word_combination(row['title']),
                'description': row['description'],
                'is_abstract': row['is_abstract'],
                'is_final': row['is_final']
            }

            table_name = common.concept_name_to_table_name(
                name, catenate=False)
            table = tables.get(table_name)

            if not table:
                msg = 'internal metadata incosistency'
                details = 'Record for concept {!r} exists but ' \
                          'the table is missing'.format(name)
                raise s_err.SchemaError(msg, details=details)

            visited_tables.add(table_name)

            bases = await self.pg_table_inheritance_to_bases(
                table['name'], table['schema'], self.table_cache)

            basemap[name] = bases

            concept = s_concepts.Concept(
                name=name, title=concept['title'],
                description=concept['description'],
                is_abstract=concept['is_abstract'],
                is_final=concept['is_final'])

            schema.add(concept)

        for concept in schema.get_objects(type='concept'):
            try:
                bases = basemap[concept.name]
            except KeyError:
                pass
            else:
                concept.bases = [schema.get(b) for b in bases]

        tabdiff = set(tables.keys()) - visited_tables
        if tabdiff:
            msg = 'internal metadata incosistency'
            details = 'Extraneous data tables exist: {}'.format(
                ', '.join('"%s.%s"' % t for t in tabdiff))
            raise s_err.SchemaError(msg, details=details)

    async def order_concepts(self, schema):
        indexes = await self.read_indexes()

        sql_decompiler = decompiler.Decompiler()

        g = {}
        for concept in schema.get_objects(type='concept'):
            g[concept.name] = {"item": concept, "merge": [], "deps": []}
            if concept.bases:
                g[concept.name]["merge"].extend(b.name for b in concept.bases)

        topological.normalize(
            g, merger=s_concepts.Concept.merge, schema=schema)

        for concept in schema.get_objects(type='concept'):
            concept.finalize(schema)

            table_name = common.get_table_name(concept, catenate=False)

            tabidx = indexes.get(table_name)
            if tabidx:
                for index, index_sql in tabidx:
                    if index.get_metadata('ddl:inherited'):
                        continue

                    ir_tree = sql_decompiler.transform(index_sql, concept)
                    edgeql_tree = edgeql.decompile_ir(
                        ir_tree, return_statement=True)
                    expr = edgeql.generate_source(edgeql_tree, pretty=False)
                    schema_name = index.get_metadata('schemaname')
                    index = s_indexes.SourceIndex(
                        name=sn.Name(schema_name), subject=concept, expr=expr)
                    concept.add_index(index)
                    schema.add(index)

    def normalize_domain_descr(self, d):
        if d['basetype'] is not None:
            typname, typmods = self.parse_pg_type(d['basetype_full'])
            result = self.pg_type_to_atom_name_and_constraints(
                typname, typmods)
            if result:
                base, constr = result

        if d['default'] is not None:
            d['default'] = self.interpret_sql(d['default'])

        return d

    async def pg_table_inheritance(self, table_name, schema_name):
        inheritance = introspection.tables.TableInheritance(self.connection)
        inheritance = await inheritance.fetch(
            table_name=table_name, schema_name=schema_name, max_depth=1)
        return tuple(i[:2] for i in inheritance[1:])

    async def pg_table_inheritance_to_bases(
            self, table_name, schema_name, table_to_concept_map):
        bases = []

        for table in await self.pg_table_inheritance(table_name, schema_name):
            base = table_to_concept_map[tuple(table[:2])]
            bases.append(base['name'])

        return tuple(bases)

    def parse_pg_type(self, type_expr):
        tree = self.parser.parse('None::' + type_expr)
        typname, typmods = self.type_expr.match(tree)
        return typname, typmods

    def pg_type_to_atom_name_and_constraints(self, typname, typmods):
        typeconv = types.base_type_name_map_r.get(typname)
        if typeconv:
            if isinstance(typeconv, sn.Name):
                name = typeconv
                constraints = ()
            else:
                name, constraints = typeconv(
                    self.connection, typname, *typmods)
            return name, constraints
        return None

    def atom_from_pg_type(self, type_expr, atom_schema, atom_default, schema):

        typname, typmods = self.parse_pg_type(type_expr)
        if isinstance(typname, tuple):
            domain_name = typname[-1]
        else:
            domain_name = typname
            if atom_schema != common.edgedb_module_name_to_schema_name('std'):
                typname = (atom_schema, typname)
        atom_name = self.domain_to_atom_map.get((atom_schema, domain_name))

        if atom_name:
            atom = schema.get(atom_name, None)
        else:
            atom = None

        if not atom:

            typeconv = self.pg_type_to_atom_name_and_constraints(
                typname, typmods)
            if typeconv:
                name, _ = typeconv
                atom = schema.get(name)
                atom.acquire_ancestor_inheritance(schema)

        assert atom
        return atom

    def json_to_word_combination(self, data):
        if data:
            return morphology.WordCombination.from_dict(json.loads(data))
        else:
            return None

    def _register_record_info(self, record_info):
        self._record_mapping_cache[record_info.id] = record_info

    def _get_record_info_by_id(self, record_id):
        return self._record_mapping_cache.get(record_id)

    async def translate_pg_error(self, query, error):
        return await ErrorMech._interpret_db_error(
            self, self._constr_mech, self._type_mech, error)


async def open_database(pgconn):
    bk = Backend(pgconn)
    await bk.getschema()
    return bk
