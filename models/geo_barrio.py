# models/geo_barrio.py
from odoo import models, fields, api
import json
from shapely.geometry import shape, Point
import logging

_logger = logging.getLogger(__name__)
class GeoBarrio(models.Model):
    _name = 'geo.barrio'
    _description = 'Barrio Geográfico'
    _order = 'name'

    name = fields.Char(string='Nombre', required=True)
    code = fields.Char(string='Código')

    active = fields.Boolean(default=True)

    # GeoJSON del polígono
    polygon_geojson = fields.Text(string='Polígono (GeoJSON)', required=True)

    # Opcional: color para mapas/reportes
    color = fields.Integer(string='Color')

    def get_polygon(self):
        self.ensure_one()
    
        if not self.polygon_geojson:
            return None
    
        try:
            data = json.loads(self.polygon_geojson)
    
            if data.get('type') == 'Feature':
                geom = data.get('geometry')
            else:
                geom = data
    
            return shape(geom)
    
        except Exception as e:
            _logger.warning("❌ GeoJSON inválido en barrio %s: %s", self.name, str(e))
            return None
            
    @api.model
    def _get_barrio_from_coords(self, lat, lon):
        from shapely.geometry import Point
    
        point = Point(lon, lat)
    
        barrios = self.env['geo.barrio'].search([('active', '=', True)])
    
        for barrio in barrios:
            polygon = barrio.get_polygon()
            if polygon and polygon.buffer(0.0001).contains(point):
                return barrio
    
        return False