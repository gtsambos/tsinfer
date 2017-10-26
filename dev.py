import numpy as np
import random
import os
import h5py
import sys


import tsinfer
import msprime



def make_errors(v, p):
    """
    For each sample an error occurs with probability p. Errors are generated by
    sampling values from the stationary distribution, that is, if we have an
    allele frequency of f, a 1 is emitted with probability f and a
    0 with probability 1 - f. Thus, there is a possibility that an 'error'
    will in fact result in the same value.
    """
    w = np.copy(v)
    if p > 0:
        m = v.shape[0]
        frequency = np.sum(v) / m
        # Randomly choose samples with probability p
        samples = np.where(np.random.random(m) < p)[0]
        # Generate observations from the stationary distribution.
        errors = (np.random.random(samples.shape[0]) < frequency).astype(int)
        w[samples] = errors
    return w


def generate_samples(ts, error_p):
    """
    Returns samples with a bits flipped with a specified probability.

    Rejects any variants that result in a fixed column.
    """
    S = np.zeros((ts.sample_size, ts.num_mutations), dtype=np.int8)
    for variant in ts.variants():
        done = False
        # Reject any columns that have no 1s or no zeros
        while not done:
            S[:, variant.index] = make_errors(variant.genotypes, error_p)
            s = np.sum(S[:, variant.index])
            done = 0 < s < ts.sample_size
    return S

def make_input_hdf5(filename, samples, positions, recombination_rate, sequence_length):
    """
    Builds a HDF5 file suitable for input into the C interface.
    """
    root = h5py.File(filename, "w")
    root.attrs["format_name"] = b"tsinfer"
    root.attrs["format_version"] = (0, 1)
    root.attrs["sequence_length"] = sequence_length

    num_samples, num_sites = samples.shape
    sites_group = root.create_group("sites")
    sites_group.create_dataset("position", (num_sites, ), data=positions, dtype=float)
    sites_group.create_dataset("recombination_rate", (num_sites, ),
            data=recombination_rate, dtype=float)

    samples_group = root.create_group("samples")
    samples_group.create_dataset(
        "haplotypes", (num_samples, num_sites), data=samples, dtype=np.int8)
    root.close()

def tsinfer_dev(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        error_rate=0, method="C", log_level="WARNING",
        progress=False):

    np.random.seed(seed)
    random.seed(seed)
    L_megabases = int(L * 10**6)

    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=seed)
    print("num_sites = ", ts.num_sites)
    if ts.num_sites == 0:
        print("zero sites; skipping")
        return
    positions = np.array([site.position for site in ts.sites()])
    S = generate_samples(ts, error_rate)
    # print(S)

    recombination_rate = np.zeros_like(positions) + recombination_rate

    # hdf5 = make_input_hdf5("tmp.hdf5", S, positions, recombination_rate, L_megabases)
    # print(S)

    tsp = tsinfer.infer(
        S, positions, L_megabases, recombination_rate, error_rate,
        num_threads=num_threads, method=method, log_level=log_level, progress=progress)
    # print(tsp.tables)
    # for t in tsp.trees():
    #     print("tree", t.index)
    #     print(t.draw(format="unicode"))

    Sp = np.zeros((tsp.sample_size, tsp.num_sites), dtype="i1")
    for variant in tsp.variants():
        Sp[:, variant.index] = variant.genotypes
    assert np.all(Sp == S)
    print("DONE")


def analyse_file(filename):
    ts = msprime.load(filename)

    num_children = np.zeros(ts.num_edgesets, dtype=np.int)
    for j, e in enumerate(ts.edgesets()):
        # print(e.left, e.right, e.parent, ts.time(e.parent), e.children, sep="\t")
        num_children[j] = len(e.children)

    print("total edgesets = ", ts.num_edgesets)
    print("non binary     = ", np.sum(num_children > 2))
    print("max children   = ", np.max(num_children))
    print("mean children  = ", np.mean(num_children))

    # for l, r_out, r_in in ts.diffs():
    #     print(l, len(r_out), len(r_in), sep="\t")

    # for t in ts.trees():
    #     t.draw(
    #         "tree_{}.svg".format(t.index), 4000, 4000, show_internal_node_labels=False,
    #         show_leaf_node_labels=False)
    #     if t.index == 10:
    #         break

def large_profile():
    input_file = "tmp__NOBACKUP__/large-input.hdf5"
    if not os.path.exists(input_file):
        n = 10**4
        L = 50 * 10**6
        ts = msprime.simulate(
            n, length=L, Ne=10**4, recombination_rate=1e-8, mutation_rate=1e-8)
        S = np.zeros((ts.sample_size, ts.num_mutations), dtype=np.int8)
        for v in ts.variants():
            S[:, v.index] = v.genotypes
        positions = np.array([site.position for site in ts.sites()])
        recombination_rate = np.zeros(ts.num_sites) + 1e-8
        make_input_hdf5(input_file, S, positions, recombination_rate, ts.sequence_length)

    hdf5 = h5py.File(input_file, "r")
    tsp = tsinfer.infer(
        samples=hdf5["samples/haplotypes"][:],
        positions=hdf5["sites/position"][:],
        recombination_rate=hdf5["sites/recombination_rate"][:],
        sequence_length=hdf5.attrs["sequence_length"],
        num_threads=8, log_level="DEBUG", progress=True)
    # print(tsp.tables)
    # for t in tsp.trees():
    #     print("tree", t.index)
    #     print(t.draw(format="unicode"))

def save_ancestor_ts(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        error_rate=0, method="C", log_level="WARNING"):
    L_megabases = int(L * 10**6)
    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=seed)
    print("num_sites = ", ts.num_sites)
    positions = np.array([site.position for site in ts.sites()])
    S = generate_samples(ts, 0)
    recombination_rate = np.zeros_like(positions) + recombination_rate

    # make_input_hdf5("ancestor_example.hdf5", S, positions, recombination_rate,
    #         ts.sequence_length)

    manager = tsinfer.InferenceManager(
        S, positions, ts.sequence_length, recombination_rate,
        num_threads=num_threads, method=method, progress=True, log_level="INFO",
        ancestor_traceback_file_pattern="tmp__NOBACKUP__/tracebacks/tb_{}.pkl")

    manager.initialise()
    manager.process_ancestors()
    ts = manager.get_tree_sequence()
    ts.dump("tmp__NOBACKUP__/ancestor_ts-{}.hdf5".format(ts.num_sites))


def examine_ancestor_ts(filename):
    ts = msprime.load(filename)
    print("num_sites = ", ts.num_sites)
    print("num_trees = ", ts.num_trees)
    print("num_edges = ", ts.num_edges)

    # zero_edges = 0
    # edges = msprime.EdgeTable()
    # for e in ts.edges():
    #     if e.parent == 0:
    #         zero_edges += 1
    #     else:
    #         edges.add_row(e.left, e.right, e.parent, e.child)
    # print("zero_edges = ", zero_edges, zero_edges / ts.num_edges)
    # t = ts.tables
    # t.edges = edges
    # ts = msprime.load_tables(**t.asdict())
    # print("num_sites = ", ts.num_sites)
    # print("num_trees = ", ts.num_trees)
    # print("num_edges = ", ts.num_edges)

    # for t in ts.trees():
    #     print("Tree:", t.interval)
    #     print(t.draw(format="unicode"))
    #     print("=" * 200)

    import pickle
    j = 812
    filename = "tmp__NOBACKUP__/tracebacks/tb_{}.pkl".format(j)
    with open(filename, "rb") as f:
        debug = pickle.load(f)

    tracebacks = debug["traceback"]
    # print("focal = ", debug["focal_sites"])
    del debug["traceback"]
    print("debug:", debug)
    a = debug["ancestor"]
    lengths = [len(t) for t in tracebacks]
    import matplotlib as mp
    # Force matplotlib to not use any Xwindows backend.
    mp.use('Agg')
    import matplotlib.pyplot as plt

    plt.clf()
    plt.plot(lengths)
    plt.savefig("tracebacks_{}.png".format(j))

#     start = 0
#     for j, t in enumerate(tracebacks[start:]):
#         print("TB", j, len(t))
#         for k, v in t.items():
#             print("\t", k, "\t", v)

#     site_id = 0
#     for t in ts.trees():
#         for site in t.sites():
#             L = tracebacks[site_id]
#             site_id += 1
#         # print("TREE")
#             print(L)
#             # for x1 in L.values():
#             #     for x2 in L.values():
#             #         print("\t", x1, x2, x1 == x2, sep="\t")
#             print("SITE = ", site_id)
#             print("root children = ", len(t.children(t.root)))
#             for u, v in L.items():
#                 path = []
#                 while u != msprime.NULL_NODE:
#                     path.append(u)
#                     u = t.parent(u)
#                     # if u in L and L[u] == v:
#                     #     print("ERROR", u)
#                 print(v, path)
#             print()
#             node_labels = {u: "{}:{:.2G}".format(u, L[u]) for u in L.keys()}
#             if site_id == 694:
#                 print(t.draw(format="unicode", node_label_text=node_labels))


    # for j, L in enumerate(tracebacks):
    #     print(j, L)
        # if len(L) > max_len:
        #     max_len = len(L)
        #     max_index = j
    # # print(j, "\t", L)
    # print("max len = ", max_len)
    # for k, v in tracebacks[max_index].items():
        # print(k, "\t", v)



if __name__ == "__main__":

    np.set_printoptions(linewidth=20000)
    np.set_printoptions(threshold=20000000)

    # large_profile()
    # save_ancestor_ts(100, 1, 1, recombination_rate=1, num_threads=2)
    # examine_ancestor_ts(sys.argv[1])

    for seed in range(1, 10000):
        print(seed)
        tsinfer_dev(20, 0.3, seed=seed, error_rate=0.0, method="P")

    # tsinfer_dev(60, 1000, num_threads=5, seed=1, error_rate=0.1, method="C",
    #         log_level="INFO", progress=True)
    # for seed in range(1, 1000):
    #     print(seed)
    #     tsinfer_dev(36, 10, seed=seed, error_rate=0.1, method="python")
