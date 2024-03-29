import networkx as nx
import itertools
from collections import defaultdict, OrderedDict
from .errors import DataJointError


class Dependencies(nx.DiGraph):
    """
    The graph of dependencies (foreign keys) between loaded tables.

    Note: the 'connnection' argument should normally be supplied;
    Empty use is permitted to facilliate use of networkx algorithms which
    internally create objects with the expectation of empty constructors.
    See also: https://github.com/datajoint/datajoint-python/pull/443
    """
    def __init__(self, connection=None):
        self._conn = connection
        self._node_alias_count = itertools.count()
        super().__init__(self)

    def load(self):
        """
        Load dependencies for all loaded schemas.
        This method gets called before any operation that requires dependencies: delete, drop, populate, progress.
        """

        # reload from scratch to prevent duplication of renamed edges
        self.clear()

        # load primary key info
        if self._conn.conn_info['port'] == 'sqlite':
            tablesAndKeys = self._conn.query("""
                SELECT 
                    m.name, p.name 
                FROM 
                    sqlite_master m 
                    JOIN pragma_table_info(m.name) p 
                WHERE m.type='table' AND m.name NOT LIKE '~%%' AND p.pk=1;
                """).fetchall()

            pks = defaultdict(set)
            # in place comprehension
            if len(self._conn.schemas)>1:
                raise Exception("need to deal with sqlite and multiple schemas...")

            [pks['`{schema}`.`{table_name}`'.format(schema="','".join(self._conn.schemas), table_name=tbl)].add(col) for tbl, col in tablesAndKeys]


        else:
            keys = self._conn.query("""
                    SELECT
                        concat('`', table_schema, '`.`', table_name, '`') as tab, column_name
                    FROM information_schema.key_column_usage
                    WHERE table_name not LIKE "~%%" AND table_schema in ('{schemas}') AND constraint_name="PRIMARY"
                    """.format(schemas="','".join(self._conn.schemas)))
            pks = defaultdict(set)
            for key in keys:
                pks[key[0]].add(key[1])

        # add nodes to the graph
        for n, pk in pks.items():
            self.add_node(n, primary_key=pk)

        # load foreign keys
        if self._conn.conn_info['port'] == 'sqlite':
            fkInfos = self._conn.query("""SELECT 
                m.name AS referencing_table, 
                p.'table' AS referenced_table,
                p.'from' AS column_name,
                p.'to' AS referenced_column_name
            FROM
                sqlite_master m
                JOIN pragma_foreign_key_list(m.name) p 
            WHERE m.type = 'table'
            ORDER BY m.name
            ;""", as_dict=True)
            fks = defaultdict(lambda: dict(attr_map=OrderedDict()))
            for fkInfo in fkInfos:
                fkInfo['constraint_name'] = 'FOREIGN' # errm... temporary?
                fkInfo['referencing_table'] = '`{schema}`.`{table_name}`'.format(schema="','".join(self._conn.schemas), table_name=fkInfo['referencing_table'])
                fkInfo['referenced_table'] = '`{schema}`.`{table_name}`'.format(schema="','".join(self._conn.schemas), table_name=fkInfo['referenced_table'])
                d = fks[(fkInfo['constraint_name'], fkInfo['referencing_table'], fkInfo['referenced_table'])]
                d['referencing_table'] = fkInfo['referencing_table']
                d['referenced_table'] = fkInfo['referenced_table']
                d['attr_map'][fkInfo['column_name']] = fkInfo['referenced_column_name']
        else:
            keys = self._conn.query("""
            SELECT constraint_name,
                concat('`', table_schema, '`.`', table_name, '`') as referencing_table,
                concat('`', referenced_table_schema, '`.`',  referenced_table_name, '`') as referenced_table,
                column_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE referenced_table_name NOT LIKE "~%%" AND (referenced_table_schema in ('{schemas}') OR
                referenced_table_schema is not NULL AND table_schema in ('{schemas}'))
            """.format(schemas="','".join(self._conn.schemas)), as_dict=True)
            fks = defaultdict(lambda: dict(attr_map=OrderedDict()))
            for key in keys:
                d = fks[(key['constraint_name'], key['referencing_table'], key['referenced_table'])]
                d['referencing_table'] = key['referencing_table']
                d['referenced_table'] = key['referenced_table']
                d['attr_map'][key['column_name']] = key['referenced_column_name']

        # add edges to the graph
        for fk in fks.values():
            props = dict(
                primary=set(fk['attr_map']) <= set(pks[fk['referencing_table']]),
                attr_map=fk['attr_map'],
                aliased=any(k != v for k, v in fk['attr_map'].items()),
                multi=set(fk['attr_map']) != set(pks[fk['referencing_table']]))
            if not props['aliased']:
                self.add_edge(fk['referenced_table'], fk['referencing_table'], **props)
            else:
                # for aliased dependencies, add an extra node in the format '1', '2', etc
                alias_node = '%d' % next(self._node_alias_count)
                self.add_node(alias_node)
                self.add_edge(fk['referenced_table'], alias_node, **props)
                self.add_edge(alias_node, fk['referencing_table'], **props)

        if not nx.is_directed_acyclic_graph(self):  # pragma: no cover
            raise DataJointError('DataJoint can only work with acyclic dependencies')

    def parents(self, table_name, primary=None):
        """
        :param table_name: `schema`.`table`
        :param primary: if None, then all parents are returned. If True, then only foreign keys composed of
            primary key attributes are considered.  If False, the only foreign keys including at least one non-primary
            attribute are considered.
        :return: dict of tables referenced by the foreign keys of table
        """
        return {p[0]: p[2] for p in self.in_edges(table_name, data=True)
                if primary is None or p[2]['primary'] == primary}

    def children(self, table_name, primary=None):
        """
        :param table_name: `schema`.`table`
        :param primary: if None, then all children are returned. If True, then only foreign keys composed of
            primary key attributes are considered.  If False, the only foreign keys including at least one non-primary
            attribute are considered.
        :return: dict of tables referencing the table through foreign keys
        """
        return {p[1]: p[2] for p in self.out_edges(table_name, data=True)
                if primary is None or p[2]['primary'] == primary}

    def descendants(self, full_table_name):
        """
        :param full_table_name:  In form `schema`.`table_name`
        :return: all dependent tables sorted in topological order.  Self is included.
        """
        nodes = self.subgraph(
            nx.algorithms.dag.descendants(self, full_table_name))

        return [full_table_name] + list(
            nx.algorithms.dag.topological_sort(nodes))

    def ancestors(self, full_table_name):
        """
        :param full_table_name:  In form `schema`.`table_name`
        :return: all dependent tables sorted in topological order.  Self is included.
        """
        nodes = self.subgraph(
            nx.algorithms.dag.ancestors(self, full_table_name))
        return [full_table_name] + list(reversed(list(
            nx.algorithms.dag.topological_sort(nodes))))
