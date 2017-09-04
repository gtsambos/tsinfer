# TODO copyright and license.
"""
TODO module docs.
"""

import collections

# TODO remove this dependency. It's not used for anything important.
import attr

import numpy as np
import tqdm
import humanize
import daiquiri
import msprime

import _tsinfer


def infer(samples, positions, length, recombination_rate, error_rate, method="C",
        num_threads=1, progress=False, log_level="WARNING"):
    # Primary entry point.

    daiquiri.setup(level=log_level)
    logger = daiquiri.getLogger()
    num_samples, num_sites = samples.shape
    logger.info("Staring infer for {} samples and {} sites".format(num_samples, num_sites))

    if method == "C":
        ancestor_builder = _tsinfer.AncestorBuilder(samples, positions)
        ts_builder = _tsinfer.TreeSequenceBuilder(num_sites, 10**6, 10**6)
        matcher = _tsinfer.AncestorMatcher(ts_builder, recombination_rate)
    else:
        ancestor_builder = AncestorBuilder(samples, positions)
        ts_builder = TreeSequenceBuilder(num_sites)
        matcher = AncestorMatcher()

    frequency_classes = ancestor_builder.get_frequency_classes()
    # TODO this time is out by 1 I think.
    root_time = frequency_classes[0][0] + 1
    ts_builder.update(1, root_time, [], [], [], [], [], [])
    a = np.zeros(num_sites, dtype=np.int8)

    for age, ancestor_focal_sites in frequency_classes:
        e_left = []
        e_right = []
        e_parent = []
        e_child = []
        s_site = []
        s_node = []
        node = ts_builder.num_nodes
        for focal_sites in ancestor_focal_sites:
            ancestor_builder.make_ancestor(focal_sites, a)
            for s in focal_sites:
                assert a[s] == 1
                a[s] = 0
                s_site.append(s)
                s_node.append(node)
            # When we update this API we should pass in arrays for left, right and
            # parent. There's no point in passing child, since we already know what
            # it is. We don't need to pass the 'node' parameter here then.
            edges = matcher.find_path(node, a)
            for left, right, parent, child in zip(*edges):
                e_left.append(left)
                e_right.append(right)
                e_parent.append(parent)
                e_child.append(child)
            node += 1
        ts_builder.update(
            len(ancestor_focal_sites), age,
            e_left, e_right, e_parent, e_child,
            s_site, s_node)

    ts = finalise(ts_builder)

    return ts

def finalise(tsb):

    nodes = msprime.NodeTable()
    flags = np.zeros(tsb.num_nodes, dtype=np.uint32)
    flags[:] = 1
    time = np.zeros(tsb.num_nodes, dtype=np.float64)
    tsb.dump_nodes(flags=flags, time=time)
    nodes.set_columns(flags=flags, time=time)

    edgesets = msprime.EdgesetTable()
    left = np.zeros(tsb.num_edges, dtype=np.float64)
    right = np.zeros(tsb.num_edges, dtype=np.float64)
    parent = np.zeros(tsb.num_edges, dtype=np.int32)
    child = np.zeros(tsb.num_edges, dtype=np.int32)
    tsb.dump_edges(left=left, right=right, parent=parent, child=child)
    edgesets.set_columns(
        left=left, right=right, parent=parent, children=child,
        children_length=np.ones(tsb.num_edges, dtype=np.uint32))

    sites = msprime.SiteTable()
    sites.set_columns(
        position=np.arange(tsb.num_sites),
        ancestral_state=np.zeros(tsb.num_sites, dtype=np.int8) + ord('0'),
        ancestral_state_length=np.ones(tsb.num_sites, dtype=np.uint32))
    mutations = msprime.MutationTable()
    site = np.zeros(tsb.num_mutations, dtype=np.int32)
    node = np.zeros(tsb.num_mutations, dtype=np.int32)
    derived_state = np.zeros(tsb.num_mutations, dtype=np.int8)
    tsb.dump_mutations(site=site, node=node, derived_state=derived_state)
    derived_state += ord('0')
    mutations.set_columns(
        site=site, node=node, derived_state=derived_state,
        derived_state_length=np.ones(tsb.num_mutations, dtype=np.uint32))

    msprime.sort_tables(nodes, edgesets, sites=sites, mutations=mutations)
    # print("SORTED")
    # print(nodes)
    # print(edgesets)
    # print(sites)
    # print(mutations)

    samples = np.where(nodes.flags == 1)[0].astype(np.int32)
    # print("simplify:")
    # print(samples)
    msprime.simplify_tables(samples, nodes, edgesets)
    # print(nodes)
    # print(edgesets)
    ts = msprime.load_tables(
        nodes=nodes, edgesets=edgesets, sites=sites, mutations=mutations)
    return ts


###############################################################
#
# Python algorithm implementation.
#
# This isn't meant to be used for any real inference, as it is
# *many* times slower than the real C implementation. However,
# it is a useful development and debugging tool, and so any
# updates made to the low-level C engine should be made here
# first.
#
###############################################################


@attr.s
class Edge(object):
    left = attr.ib(default=None)
    right = attr.ib(default=None)
    parent = attr.ib(default=None)
    child = attr.ib(default=None)
    marked = attr.ib(default=False)


@attr.s
class Site(object):
    id = attr.ib(default=None)
    frequency = attr.ib(default=None)


class AncestorBuilder(object):
    """
    Builds inferred ancestors.
    """
    def __init__(self, S, positions):
        self.haplotypes = S
        self.num_samples = S.shape[0]
        self.num_sites = S.shape[1]
        self.sites = [None for j in range(self.num_sites)]
        self.sorted_sites = [None for j in range(self.num_sites)]
        for j in range(self.num_sites):
            self.sites[j] = Site(j, np.sum(S[:, j]))
            self.sorted_sites[j] = Site(j, np.sum(S[:, j]))
        self.sorted_sites.sort(key=lambda x: (-x.frequency, x.id))
        frequency_sites = collections.defaultdict(list)
        for site in self.sorted_sites:
            if site.frequency > 1:
                frequency_sites[site.frequency].append(site)
        # Group together identical sites within a frequency class
        self.frequency_classes = {}
        for frequency, sites in frequency_sites.items():
            patterns = collections.defaultdict(list)
            for site in sites:
                state = tuple(self.haplotypes[:, site.id])
                patterns[state].append(site.id)
            self.frequency_classes[frequency] = list(patterns.values())

    def get_frequency_classes(self):
        ret = []
        for frequency in reversed(sorted(self.frequency_classes.keys())):
            ret.append((frequency, self.frequency_classes[frequency]))
        return ret

    def __build_ancestor_sites(self, focal_site, sites, a):
        S = self.haplotypes
        samples = set()
        for j in range(self.num_samples):
            if S[j, focal_site.id] == 1:
                samples.add(j)
        for l in sites:
            a[l] = 0
            if self.sites[l].frequency > focal_site.frequency:
                # print("\texamining:", self.sites[l])
                # print("\tsamples = ", samples)
                num_ones = 0
                num_zeros = 0
                for j in samples:
                    if S[j, l] == 1:
                        num_ones += 1
                    else:
                        num_zeros += 1
                # TODO choose a branch uniformly if we have equality.
                if num_ones >= num_zeros:
                    a[l] = 1
                    samples = set(j for j in samples if S[j, l] == 1)
                else:
                    samples = set(j for j in samples if S[j, l] == 0)
            if len(samples) == 1:
                # print("BREAK")
                break

    def make_ancestor(self, focal_sites, a):
        # a[:] = -1
        # Setting to 0 for now to see if we can take advantage of other RLE.
        a[:] = 0
        focal_site = self.sites[focal_sites[0]]
        sites = range(focal_sites[-1] + 1, self.num_sites)
        self.__build_ancestor_sites(focal_site, sites, a)
        focal_site = self.sites[focal_sites[-1]]
        sites = range(focal_sites[0] - 1, -1, -1)
        self.__build_ancestor_sites(focal_site, sites, a)
        for j in range(focal_sites[0], focal_sites[-1] + 1):
            if j in focal_sites:
                a[j] = 1
            else:
                self.__build_ancestor_sites(focal_site, [j], a)
        return a


class TreeSequenceBuilder(object):

    def __init__(self, num_sites, replace_recombinations=False, break_polytomies=False):
        self.num_nodes = 0
        self.num_sites = num_sites
        self.time = []
        self.flags = []
        self.mutations = {}
        self.edges = []
        self.mean_traceback_size = 0
        self.replace_recombinations = replace_recombinations
        self.break_polytomies = break_polytomies

    def add_node(self, time, is_sample=True):
        self.num_nodes += 1
        self.time.append(time)
        self.flags.append(int(is_sample))
        return self.num_nodes - 1

    @property
    def num_edges(self):
        return len(self.edges)

    @property
    def num_mutations(self):
        return len(self.mutations)

    def print_state(self):
        print("TreeSequenceBuilder state")
        print("num_sites = ", self.num_sites)
        print("num_nodes = ", self.num_nodes)
        nodes = msprime.NodeTable()
        flags = np.zeros(self.num_nodes, dtype=np.uint32)
        self.dump_nodes(flags=flags, time=time)
        nodes.set_columns(flags=flags, time=time)
        print("nodes = ")
        print(nodes)

        edgesets = msprime.EdgesetTable()
        left = np.zeros(self.num_edges, dtype=np.float64)
        right = np.zeros(self.num_edges, dtype=np.float64)
        parent = np.zeros(self.num_edges, dtype=np.int32)
        child = np.zeros(self.num_edges, dtype=np.int32)
        self.dump_edges(left=left, right=right, parent=parent, child=child)
        edgesets.set_columns(
            left=left, right=right, parent=parent, children=child,
            children_length=np.ones(self.num_edges, dtype=np.uint32))
        print("edges = ")
        print(edgesets)

        if nodes.num_rows > 1:
            msprime.sort_tables(nodes, edgesets)
            samples = np.where(nodes.flags == 1)[0].astype(np.int32)
            msprime.simplify_tables(samples, nodes, edgesets)
            print("edgesets = ")
            print(edgesets)

    def _replace_recombinations(self):

        edges = sorted(self.edges, key=lambda e: (e.left, e.right, e.parent, e.child))
        last_left = edges[0].left
        last_right = edges[0].right
        last_parent=  edges[0].parent
        group_start = 0
        groups = []
        for j in range(1, len(edges)):
            condition = (
                last_left != edges[j].left or
                last_right != edges[j].right or
                last_parent != edges[j].parent)
            if condition:
                if j - group_start > 1:
                    # Exclude cases where the interval is (0, m)
                    if not (last_left == 0 and last_right == self.num_sites):
                        groups.append((group_start, j))
                group_start = j
                last_left = edges[j].left
                last_right = edges[j].right
                last_parent=  edges[j].parent
        j = len(edges)
        if j - group_start > 1:
            # Exclude cases where the interval is (0, m)
            if not (last_left == 0 and last_right == self.num_sites):
                groups.append((group_start, j))

        # print("CANDIDATES")
        candidate_edges = []
        for start, end in groups:
            for j in range(start, end):
                candidate_edges.append(edges[j])

        candidate_edges.sort(key=lambda x: (x.child, x.left, x.right))

        if len(candidate_edges) > 0:
            group_start = 0
            groups = []
            for j in range(1, len(candidate_edges)):
                condition = (
                    candidate_edges[j - 1].right != candidate_edges[j].left or
                    candidate_edges[j - 1].child != candidate_edges[j].child)
                if condition:
                    if j - group_start > 1:
                        groups.append((group_start, j))
                    group_start = j
            j = len(candidate_edges)
            if j - group_start > 1:
                groups.append((group_start, j))
            group_map = collections.defaultdict(list)

            for start, end in groups:
                # print("CANDIDATE")
                key = tuple([
                    tuple(candidate_edges[j].left for j in range(start, end)),
                    tuple(candidate_edges[j].right for j in range(start, end)),
                    tuple(candidate_edges[j].parent for j in range(start, end))])
                group_map[key].append((start, end))
                # for j in range(start, end):
                #     print("\t", candidate_edges[j])

            for key, group_list in group_map.items():
                if len(group_list) > 1:
                    # print(key)
                    last_group_parents = None
                    for start, end in group_list:
                        # print("\tGroup", start, end)
                        group_parents = []
                        for j in range(start, end):
                            if j > start:
                                assert candidate_edges[j - 1].right == candidate_edges[j].left
                                assert candidate_edges[j - 1].child == candidate_edges[j].child
                            group_parents.append(candidate_edges[j].parent)
                            # Mark the edges as removed.
                            # print("\t\t", candidate_edges[j])
                            assert not candidate_edges[j].marked
                            candidate_edges[j].marked = True
                        if last_group_parents is not None:
                            # print(last_group_parents, group_parents)
                            assert last_group_parents == group_parents
                        last_group_parents = group_parents

            # Build up the new list of edges, minus all the maked edges.
            new_edges = []
            for e in self.edges:
                if not e.marked:
                    new_edges.append(e)

            for key, group_list in group_map.items():
                print("key = ", key)
                if len(group_list) > 1:
                    # Add a new node
                    children_time = -1
                    parent_time = 1e200
                    for start, end in group_list:
                        for j in range(start, end):
                            parent_time = min(
                                parent_time, self.time[candidate_edges[j].parent])
                            children_time = max(
                                children_time, self.time[candidate_edges[j].child])
                    new_time = children_time + (parent_time - children_time) / 2
                    # TODO change this to is_sample=False when the rest is working.
                    new_node = self.add_node(new_time, is_sample=True)
                    # print("adding node ", new_node, "@time", new_time)
                    start, end = group_list[0]
                    left = candidate_edges[start].left
                    right = candidate_edges[end - 1].right
                    # For each of the segments add in a new edge
                    for j in range(start, end):
                        new_edges.append(Edge(
                            candidate_edges[j].left, candidate_edges[j].right,
                            candidate_edges[j].parent, new_node))
                        # print("j Inserting", new_edges[-1])
                    # For each child put in a new edge over the full interval.
                    for start, _ in group_list:
                        new_edges.append(Edge(
                            left, right, new_node, candidate_edges[start].child))
                        # print("s Inserting", new_edges[-1])

            self.edges = new_edges

    def insert_polytomy_ancestor(self, edges):
        """
        Insert a new ancestor for the specified edges and update the parents
        to point to this new ancestor.
        """
        # print("BREAKING POLYTOMY FOR")
        children_time = max(self.time[e.child] for e in edges)
        parent_time = self.time[edges[0].parent]
        time = children_time + (parent_time - children_time) / 2
        new_node = self.add_node(time)
        e = edges[0]
        # Add the new edge.
        self.edges.append(Edge(e.left, e.right, e.parent, new_node))
        # Update the edges to point to this new node.
        for e in edges:
            # print("\t", e)
            e.parent = new_node


    def _break_polytomies(self):
        # Gather all the egdes pointing to a given parent.
        parent_map = {}
        for e in self.edges:
            if e.parent not in parent_map:
                parent_map[e.parent] = collections.defaultdict(list)
            parent_map[e.parent][(e.left, e.right)].append(e)

        for parent, interval_map in parent_map.items():
            # If all the coordinates are identical we have nothing to do.
            if len(interval_map) > 1:
                for interval, edges in interval_map.items():
                    if len(edges) > 1:
                        self.insert_polytomy_ancestor(edges)

    def update(self, num_nodes, time, left, right, parent, child, site, node):
        for _ in range(num_nodes):
            self.add_node(time)
        for l, r, p, c in zip(left, right, parent, child):
            self.edges.append(Edge(l, r, p, c))

        for s, u in zip(site, node):
            self.mutations[s] = u

        if self.break_polytomies:
            self._break_polytomies()

        if self.replace_recombinations and len(self.edges) > 1:
            self._replace_recombinations()
        # Index the edges

        M = len(self.edges)
        self.insertion_order = sorted(
            range(M), key=lambda j: (
                self.edges[j].left, self.time[self.edges[j].parent]))
        self.removal_order = sorted(
            range(M), key=lambda j: (
                self.edges[j].right, -self.time[self.edges[j].parent]))
        # print("AFTER UPDATE")
        # self.print_state()


    def dump_nodes(self, flags, time):
        time[:] = self.time[:self.num_nodes]
        flags[:] = self.flags

    def dump_edges(self, left, right, parent, child):
        for j, edge in enumerate(self.edges):
            left[j] = edge.left
            right[j] = edge.right
            parent[j] = edge.parent
            child[j] = edge.child

    def dump_mutations(self, site, node, derived_state):
        j = 0
        for l in sorted(self.mutations.keys()):
            site[j] = l
            node[j] = self.mutations[l]
            derived_state[j] = 1
            j += 1


def is_descendant(pi, u, v):
    """
    Returns True if the specified node u is a descendent of node v. That is,
    v is on the path to root from u.
    """
    # print("IS_DESCENDENT(", u, v, ")")
    while u != v and u != msprime.NULL_NODE:
        # print("\t", u)
        u = pi[u]
    # print("END, ", u, v)
    return u == v


class AncestorMatcher(object):

    def __init__(self, tree_sequence_builder, recombination_rate):
        self.tree_sequence_builder = tree_sequence_builder
        self.recombination_rate = recombination_rate
        self.num_sites = tree_sequence_builder.num_sites

    def find_path(self, child_node, h):

        # print("best_path", h)

        M = len(self.tree_sequence_builder.edges)
        I = self.tree_sequence_builder.insertion_order
        O = self.tree_sequence_builder.removal_order
        n = self.tree_sequence_builder.num_nodes
        m = self.tree_sequence_builder.num_sites
        pi = np.zeros(n, dtype=int) - 1
        L = {u: 1.0 for u in range(n)}
        traceback = [{} for _ in range(m)]
        edges = self.tree_sequence_builder.edges

        r = 1 - np.exp(-self.recombination_rate / n)
        recomb_proba = r / n
        no_recomb_proba = 1 - r + r / n

        j = 0
        k = 0
        while j < M:
            left = edges[I[j]].left
            while edges[O[k]].right == left:
                parent = edges[O[k]].parent
                child = edges[O[k]].child
                k = k + 1
                pi[child] = -1
                if child not in L:
                    # If the child does not already have a u value, traverse
                    # upwards until we find an L value for u
                    u = parent
                    while u not in L:
                        u = pi[u]
                    L[child] = L[u]
            right = edges[O[k]].right
            while j < M and edges[I[j]].left == left:
                parent = edges[I[j]].parent
                child = edges[I[j]].child
                # print("INSERT", parent, child)
                pi[child] = parent
                j += 1
                # Traverse upwards until we find the L value for the parent.
                u = parent
                while u not in L:
                    u = pi[u]
                # The child must have an L value. If it is the same as the parent
                # we can delete it.
                if L[child] == L[u]:
                    del L[child]

            # print("END OF TREE LOOP", left, right)
            # print("left = ", left)
            # print("right = ", right)
            # print(L)
            # print(pi)
            for site in range(left, right):
                if site not in self.tree_sequence_builder.mutations:
                    traceback[site] = dict(L)
                    continue
                mutation_node = self.tree_sequence_builder.mutations[site]
                state = h[site]
                # print("Site ", site, "mutation = ", mutation_node, "state = ", state)

                # Insert an new L-value for the mutation node if needed.
                if mutation_node not in L:
                    u = mutation_node
                    while u not in L:
                        u = pi[u]
                    L[mutation_node] = L[u]
                traceback[site] = dict(L)

                # Update the likelihoods for this site.
                max_L = -1
                for v in L.keys():
                    x = L[v] * no_recomb_proba
                    assert x >= 0
                    y = recomb_proba
                    if x > y:
                        z = x
                    else:
                        z = y
                    if state == 1:
                        emission_p = int(is_descendant(pi, v, mutation_node))
                    else:
                        emission_p = int(not is_descendant(pi, v, mutation_node))
                    L[v] = z * emission_p
                    if L[v] > max_L:
                        max_L = L[v]
                assert max_L > 0

                # Normalise
                for v in L.keys():
                    L[v] /= max_L

                # Compress
                # TODO we probably don't need the second dict here and can just take
                # a copy of the keys.
                L_next = {}
                for u in L.keys():
                    if pi[u] != -1:
                        # Traverse upwards until we find another L value
                        v = pi[u]
                        while v not in L:
                            v = pi[v]
                        if L[u] != L[v]:
                            L_next[u] = L[u]
                    else:
                        L_next[u] = L[u]
                L = L_next

        u = [node for node, v in L.items() if v == 1.0][0]
        output_edge = Edge(right=m, parent=u, child=child_node)
        output_edges = [output_edge]

        # Now go back through the trees.
        j = M - 1
        k = M - 1
        # print("TRACEBACK")
        I = self.tree_sequence_builder.removal_order
        O = self.tree_sequence_builder.insertion_order
        while j >= 0:
            right = edges[I[j]].right
            while edges[O[k]].left == right:
                pi[edges[O[k]].child] = -1
                k -= 1
            left = edges[O[k]].left
            while j >= 0 and edges[I[j]].right == right:
                pi[edges[I[j]].child] = edges[I[j]].parent
                j -= 1
            # print("left = ", left, "right = ", right)
            for l in range(right - 1, max(left - 1, 0), -1):
                u = output_edge.parent
                L = traceback[l]
                v = u
                while v not in L:
                    v = pi[v]
                x = L[v]
                if x != 1.0:
                    output_edge.left = l
                    u = [node for node, v in L.items() if v == 1.0][0]
                    output_edge = Edge(right=l, parent=u, child=child_node)
                    output_edges.append(output_edge)
                assert l > 0
        self.mean_traceback_size = sum(len(t) for t in traceback) / self.num_sites
        output_edge.left = 0
        left = []
        right = []
        parent = []
        child = []
        for e in output_edges:
            left.append(e.left)
            right.append(e.right)
            parent.append(e.parent)
            child.append(e.child)
        return left, right, parent, child


