"""
Variant caller functions.
"""


import collections
import intervaltree
import numpy as np
import pandas as pd
import re

import analib
import asmlib


def get_gt(row, hap, map_tree):
    """
    Get variant genotype based on haplotype and mappability.

    :param row: Variant call row (post h1/h2 merge).
    :param hap: Haplotype being called.
    :param map_tree: Tree of mappable regions.

    :return: '1' if the variant call is in this haplotype, '0' if it was not called but is in mappable regions,
        and '.' if it is not in a mappable region.
    """

    if hap in row['HAP'].split(';'):
        return '1'

    for interval in map_tree[row['#CHROM']].overlap(row['POS'], row['END']):

        if (interval.begin <= row['POS']) and (interval.end >= row['END']):
            return '0'

    return '.'


def val_per_hap(df, df_h1, df_h2, col_name, delim=';'):
    """
    Construct a field from a merged variant DataFrame (`df`) by pulling values from each pre-merged haplotype. Matches
    the merged variant IDs from df to the correct original ID in each haplotype and returns a comma-separated string
    of values. `df_h1` and `df_h2` must be indexed with the original variant ID from the corresponding haplotype.
    Function returns a Pandas Series keyed by the `df` index.

    :param df: Merged DataFrame of variants.
    :param df_h1: Pre-merged DataFrame of variants from h1. Index must be variant IDs for the original unmerged calls.
    :param df_h2: Pre-merged DataFrame of variants from h2. Index must be variant IDs for the original unmerged calls.
    :param col_name: Get this column name from `df_h1` and `df_h2`.
    :param delim: Separate values by this delimiter.

    :return: A Pandas series keyed by indices from `df` with comma-separated values extracted from each haplotype
        DataFrame.
    """

    df_dict = {'h1': df_h1, 'h2': df_h2}

    # Generate a Series keyed by variant ID with tuples of (hap, id) for the haplotype and variant ID in that haplotype.
    # Then, apply a function to join the target values from each dataframe in the correct order.
    return df.apply(lambda row:
         tuple(zip(
             row['HAP'].split(';'), row['HAP_VARIANTS'].split(';')
         )),
         axis=1
    ).apply(
        lambda val_list: delim.join(df_dict[val[0]].loc[val[1], col_name] for val in val_list)
    )

def filter_by_tig_tree(df, tig_filter_tree):
    """
    Filter records from a callset DataFrame by matching "TIG_REGION" with regions in an IntervalTree.

    :param df: DataFrame to filter. Must contain field "TIG_REGION".
    :param tig_filter_tree: A `collections.defaultdict` of `intervaltree.IntervalTree` (indexed by contig name) of
        no-call regions. Variants with a tig region intersecting these records will be removed (any intersect). If
        `None`, then `df` is not filtered.

    :return: Filtered `df`.
    """

    if tig_filter_tree is None:
        return df

    rm_index_set = set()

    for index, row in df.iterrows():
        match_obj = re.match('^([^:]+):(\d+)-(\d+)$', row['TIG_REGION'])

        if match_obj is None:
            raise RuntimeError('Unrecognized TIG_REGION format for record {}: {}'.format(index, row['TIG_REGION']))

        if tig_filter_tree[match_obj[1]][int(match_obj[2]) - 1:int(match_obj[3])]:
            rm_index_set.add(index)

    # Return
    if not rm_index_set:
        return df

    return df.loc[[val not in rm_index_set for val in df.index]]

def merge_haplotypes(h1_file_name, h2_file_name, h1_callable, h2_callable, config_def, threads=1, chrom=None, is_inv=None):
    """
    Merge haplotypes for one variant type.

    :param h1_file_name: h1 variant call BED file name.
    :param h2_file_name: h2 variant call BED file name.
    :param h1_callable: h1 callable region BED file name.
    :param h2_callable: h2 callable region BED file name.
    :param config_def: Merge definition.
    :param threads: Number of threads for each merge.
    :param chrom: Chromosome to merge or `None` to merge all chromosomes in one step.
    :param is_inv: Add inversion columns if `True`, autodetect if `None`.

    :return: A dataframe of variant calls.
    """

    # Set is_inv
    if is_inv is None:
        is_inv = np.any(df['SVTYPE'] == 'INV')

    # Merge
    df = analib.svmerge.merge_variants(
        bed_list=[h1_file_name, h2_file_name],
        sample_names=['h1', 'h2'],
        strategy=config_def,
        threads=threads,
        subset_chrom=chrom
    )

    df.set_index('ID', inplace=True, drop=False)

    # Check is_inv
    if is_inv and not np.all(df['SVTYPE'] == 'INV'):
        raise RuntimeError('Detected inversions in merge, but not all variants are inversions ({} of {})'.format(
            np.sum(df['SVTYPE'] == 'INV'), df.shape[0]
        ))

    # Restructure columns
    if 'HAP' in df.columns:
        del (df['HAP'])

    if 'DISC_CLASS' in df.columns:
        del (df['DISC_CLASS'])

    df.columns = [re.sub('^MERGE_', 'HAP_', val) for val in df.columns]

    del (df['HAP_SRC'])
    del (df['HAP_SRC_ID'])
    del (df['HAP_AC'])
    del (df['HAP_AF'])

    df.columns = ['HAP' if val == 'HAP_SAMPLES' else val for val in df.columns]

    if df.shape[0] > 0:
        # Change , to ; from merger
        df['HAP'] = df['HAP'].apply(lambda val: ';'.join(val.split(',')))
        df['HAP_VARIANTS'] = df['HAP_VARIANTS'].apply(lambda val: ';'.join(val.split(',')))

        if 'HAP_RO' in df.columns:
            df['HAP_RO'] = df['HAP_RO'].apply(lambda val: ';'.join(val.split(',')))

        if 'HAP_OFFSET' in df.columns:
            df['HAP_OFFSET'] = df['HAP_OFFSET'].apply(lambda val: ';'.join(val.split(',')))

        if 'HAP_SZRO' in df.columns:
            df['HAP_SZRO'] = df['HAP_SZRO'].apply(lambda val: ';'.join(val.split(',')))

        if 'HAP_OFFSZ' in df.columns:
            df['HAP_OFFSZ'] = df['HAP_OFFSZ'].apply(lambda val: ';'.join(val.split(',')))

        # Add h1 and h2 to columns
        df_h1 = analib.pd.read_csv_chrom(h1_file_name, chrom=chrom, sep='\t', low_memory=False)
        df_h1.set_index('ID', inplace=True, drop=False)
        df_h1['CLUSTER_MATCH'].fillna('NA', inplace=True)
        df_h1 = df_h1.astype(str)

        df_h2 = analib.pd.read_csv_chrom(h2_file_name, chrom=chrom, sep='\t', low_memory=False)
        df_h2.set_index('ID', inplace=True, drop=False)
        df_h2['CLUSTER_MATCH'].fillna('NA', inplace=True)
        df_h2 = df_h2.astype(str)

        df['TIG_REGION'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'TIG_REGION')
        df['QUERY_STRAND'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'QUERY_STRAND')
        df['CI'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'CI')
        df['ALIGN_INDEX'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'ALIGN_INDEX')
        df['CLUSTER_MATCH'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'CLUSTER_MATCH')
        df['CALL_SOURCE'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'CALL_SOURCE')

        # Set inversion columns
        if is_inv:
            del(df['RGN_REF_DISC'])
            del(df['RGN_TIG_DISC'])
            del(df['FLAG_ID'])
            del(df['FLAG_TYPE'])

            df['RGN_REF_INNER'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'RGN_REF_INNER')
            df['RGN_TIG_INNER'] = asmlib.call.val_per_hap(df, df_h1, df_h2, 'RGN_TIG_INNER')

        # Load mapped regions
        map_tree_h1 = collections.defaultdict(intervaltree.IntervalTree)
        map_tree_h2 = collections.defaultdict(intervaltree.IntervalTree)

        df_map_h1 = pd.read_csv(h1_callable, sep='\t')
        df_map_h2 = pd.read_csv(h2_callable, sep='\t')

        for index, row in df_map_h1.iterrows():
            map_tree_h1[row['#CHROM']][row['POS']:row['END']] = True

        for index, row in df_map_h2.iterrows():
            map_tree_h2[row['#CHROM']][row['POS']:row['END']] = True

        # Get genotypes setting no-call for non-mappable regions
        df['GT_H1'] = df.apply(asmlib.call.get_gt, hap='h1', map_tree=map_tree_h1, axis=1)
        df['GT_H2'] = df.apply(asmlib.call.get_gt, hap='h2', map_tree=map_tree_h2, axis=1)

        df['GT'] = df.apply(lambda row: '{}|{}'.format(row['GT_H1'], row['GT_H2']), axis=1)

        if np.any(df['GT'].apply(lambda val: val == '0|0')):
            raise RuntimeError('Program bug: Found 0|0 genotypes after merging haplotypes')

        del df['GT_H1']
        del df['GT_H2']

    else:

        df['TIG_REGION'] = np.nan
        df['QUERY_STRAND'] = np.nan
        df['CI'] = np.nan
        df['ALIGN_INDEX'] = np.nan
        df['CLUSTER_MATCH'] = np.nan
        df['CALL_SOURCE'] = np.nan

        if is_inv:
            del(df['RGN_REF_DISC'])
            del(df['RGN_TIG_DISC'])
            del(df['FLAG_ID'])
            del(df['FLAG_TYPE'])

            df['RGN_REF_INNER'] = np.nan
            df['RGN_TIG_INNER'] = np.nan

        df['GT'] = np.nan

    # Return merged BED
    return df
