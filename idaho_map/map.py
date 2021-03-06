from jupyter_react import Component
from collections import defaultdict
import requests
import os, sys
import rasterio
from rasterio.merge import merge
import sh
from decimal import Decimal
import mercantile 
import json

from rasterio import guard_transform

#from boto.s3.connection import S3Connection
#s3 = S3Connection(profile_name='dg')

from gbdxtools import Interface
gbdx = Interface()

import tempfile
import threading

from bson.json_util import loads as json_loads
import numpy as np
import geoio.constants as constants
import ephem

def fixmeta(meta):
    return json_loads(json.dumps(meta))

def calc_toa_gain_offset(meta):
    """
    Compute (gain, offset) tuples for each band of the specified image metadata
    """

    # Set satellite index to look up cal factors
    sat_index = meta['satid'].upper() + "_" + \
                meta['bandid'].upper()

    # Set scale for at sensor radiance
    # Eq is:
    # L = GAIN * DN * (ACF/EBW) + Offset
    # ACF abscal factor from meta data
    # EBW effectiveBandwidth from meta data
    # Gain provided by abscal from const
    # Offset provided by abscal from const
    acf = np.asarray(meta['abscalfactor']) # Should be nbands length
    ebw = np.asarray(meta['effbandwidth'])  # Should be nbands length
    gain = np.asarray(constants.DG_ABSCAL_GAIN[sat_index])
    scale = (acf/ebw)*(gain)
    offset = np.asarray(constants.DG_ABSCAL_OFFSET[sat_index])

    e_sun_index = meta['satid'].upper() + "_" + \
                  meta['bandid'].upper()
    e_sun = np.asarray(constants.DG_ESUN[e_sun_index])
    sun = ephem.Sun()
    img_obs = ephem.Observer()
    img_obs.lon = meta['latlonhae'][1]
    img_obs.lat = meta['latlonhae'][0]
    img_obs.elevation = meta['latlonhae'][2]
    img_obs.date = meta['img_datetime_obj_utc']
    sun.compute(img_obs)
    d_es = sun.earth_distance

    ## Pull sun elevation from the image metadata
    #theta_s can be zenith or elevation - the calc below will us either
    # a cos or s in respectively
    #theta_s = float(self.meta_dg.IMD.IMAGE.MEANSUNEL)
    theta_s = 90-float(meta['mean_sun_el'])
    scale2 = (d_es ** 2 * np.pi) / (e_sun * np.cos(np.deg2rad(theta_s)))

    # Return scaled data
    # Radiance = Scale * Image + offset, Reflectance = Radiance * Scale2
    return zip(scale, scale2, offset)


class Map(Component):
    module = 'Map'
    features = []
    merged = []

    def __init__(self, **kwargs):
        super(Map, self).__init__(target_name='idaho.map', props=kwargs.get('props', {}))
        self.stream_config = self.props.get('stream', {})
        self.on_msg(self._handle_msg)

    def idaho_stream(self, config={"fromDate": "2015-01-01", "toDate": "2015-06-02", "delay":"0.1", "bbox": "-24.521484,21.983801,43.154297,55.002826"}):
        r = requests.post('http://idaho.timbr.io/filter', json=config, stream=True)
        g = r.iter_lines()
        return g

    def start(self, config=None):
        if config is None:
            config = self.stream_config
        stream = self.idaho_stream(config=config)
    
        def fn():
            for msg in stream:
                self.add_features([json.loads(msg)])
        
        thread = threading.Thread(target=fn)
        thread.start()

    def add_features(self, features):
        self.send({ "method": "update", "props": {"features": features}})

    def _handle_msg(self, msg):
        data = msg['content']['data']
        if data.get('method', '') == 'stitch':
            chips = defaultdict(list)
            raw = data.get('chips', {})
            for date, _chips in raw.iteritems():
              for c in _chips:
                props = c['properties']
                chips[c['id']].append(props)
            self.fetch_chips( chips )
        elif data.get('method', '') == 'save_chips':
            self.save_chips(data.get('chips', {}))

    def save_chips(self, raw_chips):
        self.chips = defaultdict(list)
        for date, chips in raw_chips.iteritems():
            for c in chips:
              props = c['properties']
              self.chips[c['id']].append(props)
        

    #def get_vrt(self, tile, idaho_id):
    #    bnds = mercantile.bounds(tile)
    #    x_res, y_res = (bnds.east - bnds.west) / 256, (bnds.south - bnds.north) / 256
    #    _bucket = s3.get_bucket('idaho-images')
    #    _rrds = json.loads(_bucket.get_key('{}/rrds.json'.format(idaho_id)).get_contents_as_string())
  
    #    level = 0
    #    for i, r in enumerate(_rrds['reducedResolutionDataset']):
    #        warp = json.loads(_bucket.get_key('{}/native_warp_spec.json'.format(r['targetImage'])).get_contents_as_string())
    #        _x, _y = (warp['targetGeoTransform']['scaleX'], warp['targetGeoTransform']['scaleY'])
    #        if _x < x_res and _y < y_res:
    #            level = i

    #    return 'http://idaho.timbr.io/{}/toa/{}.vrt'.format( idaho_id, level )

    def pixel_box(self, src, bounds):
        bbox = [bounds.east, bounds.south, bounds.west, bounds.north]
        ul = src.index(*bbox[0:2])
        lr = src.index(*bbox[2:4])
        
        if ul[0] < 0:
            ul = (0, ul[1])
        if ul[1] < 0:
            ul = (ul[0], 0)
        
        if lr[0] > src.height:
            lr = (src.width, lr[1])
        if lr[1] > src.width:
            lr = (lr[0], src.height)
            
        return ((lr[0], ul[0]+1), (lr[1], ul[1]+1))
    
    def get_chip_url(self, mid, xyz):
        base = "http://idaho.geobigdata.io/v1/tile/idaho-images/{}".format(mid)
        return "{}/{}?bands=0,1,2,3,4,5,6,7&format=tif&token={}".format(
            base, '/'.join(map(str,[xyz[2],xyz[0],xyz[1]])), gbdx.gbdx_connection.access_token)

    def get_chip(self, name, mid, data, outdir):
        url = self.get_chip_url(mid,  data['xyz'].split(','))
        path = os.path.join(outdir, name+'.tif')
        toa = os.path.join(outdir, name+'_toa.tif')
        wgs84 = os.path.join(outdir, name+'_wgs84.tif')
    
        if not os.path.exists(path):
            print 'Retrieving Chip', path
            r = requests.get(url)
            if r.status_code == 200:
                with open(path, 'wb') as the_file:
                    the_file.write(r.content)
            else:
                self.send({ "method": "update", "props": {"progress": { "status": "error", "text": 'There was a problem retrieving IDAHO Image {}'.format(url) }}})
                r.raise_for_status()

            
            meta = fixmeta(data)
            params = calc_toa_gain_offset(meta)
            scale, scale2, offset = params[0]

            with rasterio.open(path) as src:
                out_kwargs = src.meta.copy()
                out_kwargs.update({
                    'dtype': 'float32'
                })
                with rasterio.open(toa, 'w', **out_kwargs) as out:
                    out.write( (scale2 * (scale * (np.stack(src.read()).astype(np.float32)) + offset )))
    
            # georef the file 
            bounds = data['bbox']
            opts = ["-of", "GTiff", "-a_ullr", bounds[0], bounds[3], bounds[2], bounds[1], "-a_srs", "EPSG:4326",  toa, wgs84]
            try: 
              gdal_translate = sh.Command(os.path.join(os.path.dirname(sys.executable), "gdal_translate"))
            except:
              gdal_translate = sh.Command("gdal_translate")

            result = gdal_translate(*opts)

        return wgs84


    def fetch_chips(self, chips=None):
        if chips is None:
            chips = self.chips
        self.merged = []
        total = len(sum(chips.values(), []))
        current = 0
        for idaho_id in chips.keys():
            img_dir = os.path.join(os.environ.get('HOME','./'), 'gbdx', 'idaho', idaho_id)

            if not os.path.exists(img_dir):
                os.makedirs(img_dir)

            files = []
            for i, t in enumerate(chips[idaho_id]):
                current += 1 
                text = 'Fetching {} of {} chips'.format(current, total)
                self.send({ "method": "update", "props": {"progress": { "status": "processing", "percent": (float(current) / float(total)) * 100, "text": text }}})
                files.append( self.get_chip('{}_{}'.format(idaho_id, t['xyz'].replace(',','_')), idaho_id, t, img_dir) )
            self.merge_chips(files, idaho_id)
        
        self.send({ "method": "update", "props": {"progress": { "status": "complete" }}})


    def merge_chips(self, files, idaho_id):
        print 'merging', files
        sources = [rasterio.open(f) for f in files]
        dest, output_transform = merge(sources)

        profile = sources[0].profile
        profile['transform'] = output_transform
        profile['height'] = dest.shape[1]
        profile['width'] = dest.shape[2]
        profile['driver'] = 'GTiff'

        img_dir = os.path.join(os.environ.get('HOME','./'), 'gbdx', 'idaho', idaho_id)
        if not os.path.exists(img_dir):
            os.makedirs(img_dir)

        output = img_dir + '/merge.tif'
        with rasterio.open(output, 'w', **profile) as dst:
            dst.write(dest)
        self.merged.append( output )
