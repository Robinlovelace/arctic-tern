#!/usr/bin/env python
"""simplify.py: simplify GeoJSON network toGeoPKG layers using image skeletonization"""

import argparse
import datetime as dt
import warnings
from functools import partial

import geopandas as gp
import networkx as nx
import numpy as np
import pandas as pd
import rasterio as rio
import rasterio.features as rif
from pyogrio import read_dataframe, write_dataframe
from shapely import get_coordinates, line_merge, set_precision, unary_union
from shapely.affinity import affine_transform
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point
from skimage.morphology import remove_small_holes, skeletonize

TRANSFORM_ONE = np.asarray([0.0, 1.0, -1.0, 0.0, 1.0, 1.0])

pd.set_option("display.max_columns", None)
START = dt.datetime.now()
CRS = "EPSG:27700"


def combine_line(line):
    """combine_line: return LineString GeoSeries combining lines with intersecting endpoints

    args:
      line: mixed LineString GeoSeries

    returns:
      join LineString GeoSeries

    """
    r = MultiLineString(line.values)
    return gp.GeoSeries(line_merge(r).geoms, crs=CRS)


def get_base_geojson(filepath):
    """get_base_nx: return GeoDataFrame at 0.1m precision from GeoJSON

    args:
      filepath: GeoJSON path

    returns:
      GeoDataFrame at 0.1m precision

    """
    r = read_dataframe(filepath).to_crs(CRS)
    r["geometry"] = r["geometry"].map(set_precision_pointone)
    return r


def get_end(geometry):
    """get_end: return numpy array of geometry LineString end-points

    args:
      geometry: geometry LineString

    returns:
      end-point numpy arrays

    """
    r = get_coordinates(geometry)
    return np.vstack((r[0, :], r[-1, :]))


def get_geometry_buffer(this_gf, segment=5.0, radius=8.0):
    """get_geometry_buffer: return radius buffered geometry using segmented GeoDataFrame

    args:
      this_gf: GeoDataFrame to
      segment: (default value = 5.0)
      radius: (default value = 8.0)

    returns:
      buffered GeoSeries geometry

    """
    set_segment = partial(get_segment, distance=segment)
    r = this_gf.map(set_segment).explode()
    r = gp.GeoSeries(r, crs=CRS).buffer(radius, join_style="mitre")
    union = unary_union(r)
    try:
        r = gp.GeoSeries(union.geoms, crs=CRS)
    except AttributeError:
        r = gp.GeoSeries(union, crs=CRS)
    return r


def get_linestring(line):
    """get_linestring: return LineString GeoSeries from line coordinates

    args:
      line:

    returns:
       LineString GeoSeries
    """
    r = get_coordinates(line)
    r = np.stack([gp.points_from_xy(*r[:-1].T), gp.points_from_xy(*r[1:].T)])
    return gp.GeoSeries(pd.DataFrame(r.T).apply(LineString, axis=1), crs=CRS).values


def get_nx(line):
    """get_nx: return primal edge and node network from LineString GeoDataFrame

    args:
      line: LineString GeoDataFrame

    returns:
      edge, node GeoDataFrames

    """
    r = line.map(get_end)
    edge = gp.GeoSeries(r.map(LineString), crs=CRS)
    r = np.vstack(r.to_numpy())
    r = gp.GeoSeries(map(Point, r)).to_frame("geometry")
    r = r.groupby(r.columns.to_list(), as_index=False).size()
    node = gp.GeoDataFrame(r, crs=CRS)
    return edge, node


def get_segment(line, distance=50.0):
    """get_segment: segment LineString GeoSeries into distance length segments

    args:
      line: GeoSeries LineString
      length: segmentation distance (default value = 50.0)

    returns:
      GeoSeries of LineStrings of up to length distance

    """
    return get_linestring(line.segmentize(distance))


def get_source_target(line):
    """get_source_target: return edge and node GeoDataFrames from LineString with unique
    node Point and edge source and target

    args:
      line: LineString GeoDataFrame

    returns:
      edge, node: GeoDataFrames

    """
    edge = line.copy()
    r = edge["geometry"].map(get_end)
    r = np.stack(r)
    node = gp.GeoSeries(map(Point, r.reshape(-1, 2)), crs=CRS).to_frame("geometry")
    count = node.groupby("geometry").size().rename("count")
    node = node.drop_duplicates("geometry").set_index("geometry", drop=False)
    node = node.join(count).reset_index(drop=True).reset_index(names="node")
    ix = node.set_index("geometry")["node"]
    edge = edge.reset_index(names="edge")
    edge["source"] = ix.loc[map(Point, r[:, 0])].values
    edge["target"] = ix.loc[map(Point, r[:, 1])].values
    return edge, node


def log(this_string):
    """log: print timestamp appended to 'this_string'

      this_string: text to print

    returns:
      None

    """
    now = dt.datetime.now() - START
    print(this_string + f"\t{now}")


def get_dimension(bound, scale=1.0):
    """get_dimension: calculates scaled image size in px

      bound: boundary corner points
      scale: scaling factor (default = 1.0)

    returns:
      size in px

    """
    r = np.diff(bound.reshape(-1, 2), axis=0)
    r = np.ceil(r.reshape(-1))
    return (r[[1, 0]] * scale).astype(int)


def get_affine_transform(this_gf, scale=1.0):
    """get_affine_transform: return affine transformations matrices, and scaled image size
    from GeoPandas boundary size

      this_gf: GeoPanda
      scale:  (default = 1.0)

    returns:
      rasterio and shapely affine tranformation matrices, and image size in px

    """
    bound = this_gf.total_bounds
    s = TRANSFORM_ONE / scale
    s[[4, 5]] = bound[[0, 3]]
    r = s[[1, 0, 4, 3, 2, 5]]
    r = rio.Affine(*r)
    return r, s, get_dimension(bound, scale)


set_precision_pointone = partial(set_precision, grid_size=0.1)


def get_raster_point(raster, value=1):
    """get_raster_point: return Point GeoSeries from raster array with values >= value

    args:
      raster: raster numpy array
      value: point threshold (default value = 1)
    returns:
      GeoSeries Point

    """
    r = np.stack(np.where(raster >= value))
    return gp.GeoSeries(map(Point, r.T), crs=CRS)


def nx_out(this_gf, transform, filepath, layer):
    """nx_out: write transform GeoPandas data to GeoPKG layer

    args:
      this_gf: GeoDataFrame to output
      transform: affine transform
      filepath: GeoPKG filepath
      layer: layer name

    returns:
      None

    """
    r = this_gf.copy()
    try:
        r = r.to_frame("geometry")
    except AttributeError:
        pass
    geometry = r["geometry"].map(transform).map(set_precision_pointone)
    r["geometry"] = geometry
    write_dataframe(r, filepath, layer=layer)


def get_skeleton(geometry, transform, shape):
    """get_skeleton: return skeletonized raster buffer from Shapely geometry

    args:
      geometry: Shapely geometry to convert to raster buffer
      transform: rasterio affine transformation
      shape: output buffer px size

    returns:
      skeltonized numpy array raster buffer

    """
    r = rif.rasterize(geometry.values, transform=transform, out_shape=shape)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # parent, traverse = max_tree(invert(r))
        r = remove_small_holes(r, 4).astype(np.uint8)
    return skeletonize(r).astype(np.uint8)


def get_connected_class(edge_list):
    """get_connected_class: return labeled connected node pandas Series from edge list

    args:
      edge_list: source, target edge pandas DataFrame

    returns:
      labeled node pandas Series

    """
    nx_graph = nx.from_pandas_edgelist(edge_list)
    connected = nx.connected_components(nx_graph)
    r = {k: i for i, j in enumerate(connected) for k in j}
    return pd.Series(r, name="class")


def get_centre_edge(node):
    """get_centre_edge: return centroid Point from discrete node clusters

    args:
      node: discrete node cluster GeoDataSeries

    returns:
      GeoDataCentre node cluster centroid Point

    """
    centre = node[["geometry", "class"]].groupby("class").aggregate(tuple)
    centre = gp.GeoSeries(centre["geometry"].map(MultiPoint), crs=CRS).centroid
    centre = centre.rename("target")
    geometry = node[["class", "geometry"]].set_index("class").join(centre)
    geometry = geometry.apply(LineString, axis=1)
    r = node.rename(columns={"node": "source"}).copy()
    r["geometry"] = geometry.values
    return r


def get_raster_line(point, knot=False):
    """get_raster_line: return LineString GeoSeries from 1px line raster eliminating knots

    args:
      point: 1px raster array with knots

    returns:
      1px line LineString GeoSeries with knots removed

    """
    square = point.buffer(1, cap_style="square", mitre_limit=1)
    ix = point.sindex.query(square, predicate="covers").T
    ix.sort()
    s = pd.DataFrame(ix).drop_duplicates()
    s = s[s[0] != s[1]]
    s = np.stack([point[s[0].values], point[s[1].values]]).T
    r = gp.GeoSeries(map(LineString, s), crs=CRS)
    edge, node = get_source_target(combine_line(r).to_frame("geometry"))
    if knot:
        return combine_line(edge["geometry"])
    ix = edge.length > 2.0
    connected = get_connected_class(edge.loc[~ix, ["source", "target"]])
    node = node.loc[connected.index].join(connected).sort_index()
    connected_edge = get_centre_edge(node)
    r = combine_line(pd.concat([connected_edge["geometry"], edge.loc[ix, "geometry"]]))
    return r[r.length > 2.0]


def main(inpath, outpath, buffer_size, scale, knot=False):
    """main: load GeoJSON file, use skeletonize buffer to simplify network, and output
    input, simplified and primal network as GeoPKG layers

    args:
       path: GeoJSON filepath

    returns:
       None

    """
    log("start\t")
    base_nx = get_base_geojson(inpath)
    log("read geojson")
    write_dataframe(base_nx, outpath, layer="input")
    log("process\t")
    nx_geometry = get_geometry_buffer(base_nx["geometry"], radius=buffer_size)
    r_matrix, s_matrix, out_shape = get_affine_transform(nx_geometry, scale)
    shapely_transform = partial(affine_transform, matrix=s_matrix)
    skeleton_im = get_skeleton(nx_geometry, r_matrix, out_shape)
    nx_point = get_raster_point(skeleton_im)
    nx_line = get_raster_line(nx_point, knot)
    log("write simple")
    nx_out(nx_line, shapely_transform, outpath, "line")
    log("write primal")
    nx_edge, _ = get_nx(nx_line)
    nx_out(nx_edge, shapely_transform, outpath, "primal")
    log("stop\t")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GeoJSON network raster simplification"
    )
    parser.add_argument("inpath", type=str, help="GeoJSON filepath to simplify")
    parser.add_argument(
        "outpath",
        nargs="?",
        type=str,
        help="GeoGPKG output path",
        default="output.gpkg",
    )
    parser.add_argument("--scale", help="raster scale", type=float, default=1.0)
    parser.add_argument("--buffer", help="line buffer [m]", type=float, default=8.0)
    parser.add_argument("--knot", help="keep image knots", action="store_false")
    args = parser.parse_args()
    main(
        args.inpath,
        outpath=args.outpath,
        buffer_size=args.buffer,
        scale=args.scale,
        knot=args.knot,
    )
