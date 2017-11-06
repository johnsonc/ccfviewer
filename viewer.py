from ast import literal_eval
import sys, os, traceback
sys.path.append(os.path.join(os.path.dirname(__file__)))

import json
from collections import OrderedDict
import numpy as np
import pyqtgraph as pg
import pyqtgraph.metaarray as metaarray
from pyqtgraph.Qt import QtGui, QtCore
import math
import points_to_aff

from aiccf.data import CCFAtlasData
from aiccf.ui import AtlasDisplayCtrl, LabelTree, AtlasSliceView


class AtlasViewer(QtGui.QWidget):
    def __init__(self, parent=None):
        self.atlas = None
        self.label = None

        QtGui.QWidget.__init__(self, parent)
        self.layout = QtGui.QGridLayout()
        self.setLayout(self.layout)
        self.layout.setSpacing(0)
        self.layout.setContentsMargins(0,0,0,0)

        self.splitter = QtGui.QSplitter()
        self.layout.addWidget(self.splitter, 0, 0)

        self.view = VolumeSliceView()
        self.view.mouseHovered.connect(self.mouseHovered)
        self.view.mouseClicked.connect(self.mouseClicked)
        self.splitter.addWidget(self.view)
        
        self.statusLabel = QtGui.QLabel()
        self.layout.addWidget(self.statusLabel, 1, 0, 1, 1)
        self.statusLabel.setFixedHeight(30)

        self.pointLabel = QtGui.QLabel()
        self.layout.addWidget(self.pointLabel, 2, 0, 1, 1)
        self.pointLabel.setFixedHeight(30)

        self.ctrl = QtGui.QWidget(parent=self)
        self.splitter.addWidget(self.ctrl)
        self.ctrlLayout = QtGui.QVBoxLayout()
        self.ctrl.setLayout(self.ctrlLayout)

        self.displayCtrl = AtlasDisplayCtrl(parent=self)
        self.ctrlLayout.addWidget(self.displayCtrl)
        self.displayCtrl.params.sigTreeStateChanged.connect(self.displayCtrlChanged)

        self.labelTree = LabelTree(self)
        self.labelTree.labelsChanged.connect(self.labelsChanged)
        self.ctrlLayout.addWidget(self.labelTree)
        
        self.coordinateCtrl = CoordinatesCtrl(self)
        self.coordinateCtrl.coordinateSubmitted.connect(self.coordinateSubmitted)
        self.ctrlLayout.addWidget(self.coordinateCtrl)

    def set_data(self, atlas_data):
        self.atlas_data = atlas_data
        self.setAtlas(atlas_data.image)
        self.setLabels(atlas_data.label, atlas_data.ontology)

    def setLabels(self, label, ontology):
        self.label = label
        self.labelTree.set_ontology(ontology)
        self.updateImage()
        self.labelsChanged()

    def setAtlas(self, atlas):
        self.atlas = atlas
        self.coordinateCtrl.atlas_shape = atlas.shape
        self.updateImage()

    def updateImage(self):
        if self.atlas is None or self.label is None:
            return
        axis = self.displayCtrl.params['Orientation']
        axes = {
            'right': ('right', 'anterior', 'dorsal'),
            'dorsal': ('dorsal', 'right', 'anterior'),
            'anterior': ('anterior', 'right', 'dorsal')
        }[axis]
        order = [self.atlas._interpretAxis(ax) for ax in axes]

        # transpose, flip, downsample images
        ds = self.displayCtrl.params['Downsample']
        self.displayAtlas = self.atlas.view(np.ndarray).transpose(order)
        with pg.BusyCursor():
            for ax in (0, 1, 2):
                self.displayAtlas = pg.downsample(self.displayAtlas, ds, axis=ax)
        self.displayLabel = self.label.view(np.ndarray).transpose(order)[::ds, ::ds, ::ds]

        # make sure atlas/label have the same size after downsampling

        self.view.setData(self.displayAtlas, self.displayLabel, scale=self.atlas._info[-1]['vxsize']*ds)

    def labelsChanged(self):
        lut = self.labelTree.lookupTable()
        self.view.atlas_view.setLabelLUT(lut)        
        
    def displayCtrlChanged(self, param, changes):
        update = False
        for param, change, value in changes:
            if param.name() == 'Composition':
                self.view.setOverlay(value)
            elif param.name() == 'Opacity':
                self.view.setLabelOpacity(value)
            elif param.name() == 'Interpolate':
                self.view.setInterpolation(value)
            else:
                update = True
        if update:
            self.updateImage()

    def mouseHovered(self, id):
        self.statusLabel.setText(self.labelTree.describe(id))
        
    def renderVolume(self):
        import pyqtgraph.opengl as pgl
        import scipy.ndimage as ndi
        self.glView = pgl.GLViewWidget()
        img = np.ascontiguousarray(self.displayAtlas[::8,::8,::8])
        
        # render volume
        #vol = np.empty(img.shape + (4,), dtype='ubyte')
        #vol[:] = img[..., None]
        #vol = np.ascontiguousarray(vol.transpose(1, 2, 0, 3))
        #vi = pgl.GLVolumeItem(vol)
        #self.glView.addItem(vi)
        #vi.translate(-vol.shape[0]/2., -vol.shape[1]/2., -vol.shape[2]/2.)
        
        verts, faces = pg.isosurface(ndi.gaussian_filter(img.astype('float32'), (2, 2, 2)), 5.0)
        md = pgl.MeshData(vertexes=verts, faces=faces)
        mesh = pgl.GLMeshItem(meshdata=md, smooth=True, color=[0.5, 0.5, 0.5, 0.2], shader='balloon')
        mesh.setGLOptions('additive')
        mesh.translate(-img.shape[0]/2., -img.shape[1]/2., -img.shape[2]/2.)
        self.glView.addItem(mesh)

        self.glView.show()
     
    # mouse_point[0] contains the Point object.
    # mouse_point[1] contains the structure id at Point
    def mouseClicked(self, mouse_point):
        point, to_clipboard = self.getCcfPoint(mouse_point)
        self.pointLabel.setText(point)
        self.view.target.setVisible(True)
        self.view.target.setPos(self.view.view2.mapSceneToView(mouse_point[0].scenePos()))
        self.view.clipboard.setText(to_clipboard)

    # Get CCF point coordinate and Structure id
    # Returns two strings. One used for display in a label and the other to put in the clipboard
    # PIR orientation where x axis = Anterior-to-Posterior, y axis = Superior-to-Inferior and z axis = Left-to-Right
    def getCcfPoint(self, mouse_point):

        axis = self.displayCtrl.params['Orientation']

        # find real lims id
        lims_str_id = (key for key, value in self.label._info[-1]['ai_ontology_map'] if value == mouse_point[1]).next()
        
        # compute the 4x4 transform matrix
        a = self.scale_point_to_CCF(self.view.line_roi.origin)
        ab = self.scale_vector_to_PIR(self.view.line_roi.ab_vector)
        ac = self.scale_vector_to_PIR(self.view.line_roi.ac_vector)
        
        M0, M0i = points_to_aff.points_to_aff(a, np.array(ab), np.array(ac))

        # Find what the mouse point position is relative to the coordinate
        ab_length = np.linalg.norm(self.view.line_roi.ab_vector)
        ac_length = np.linalg.norm(self.view.line_roi.ac_vector)        
        p = (mouse_point[0].pos().x()/ac_length, mouse_point[0].pos().y()/ab_length)
        
        ccf_location = np.dot(M0i, [p[1], p[0], 0, 1]) # use the inverse transform matrix and the mouse point
        
        # These should be x, y, z
        p1 = float(ccf_location[0])
        p2 = float(ccf_location[1])
        p3 = float(ccf_location[2])

        if axis == 'right':
            point = "x: " + str(p1) + " y: " + str(p2) + " z: " + str(p3) + " StructureID: " + str(lims_str_id)
            clipboard_text = str(p1) + ";" + str(p2) + ";" + str(p3) + ";" + str(lims_str_id)
        elif axis == 'anterior':
            point = "x: " + str(p3) + " y: " + str(p2) + " z: " + str(p1) + " StructureID: " + str(lims_str_id)
            clipboard_text = str(p3) + ";" + str(p2) + ";" + str(p1) + ";" + str(lims_str_id)
        elif axis == 'dorsal':
            point = "x: " + str(p2) + " y: " + str(p3) + " z: " + str(p1) + " StructureID: " + str(lims_str_id)
            clipboard_text = str(p2) + ";" + str(p3) + ";" + str(p1) + ";" + str(lims_str_id)
        else:
            point = 'N/A'
            clipboard_text = 'NULL'

        # Convert matrix transform to a LIMS dictionary
        ob = points_to_aff.aff_to_lims_obj(M0, M0i)

        # These are just for testing
        # roi_origin_position = (self.view.line_roi.pos().x(), self.view.line_roi.pos().y())
        # roi_size = (self.view.line_roi.size().x(), self.view.line_roi.size().y())
        # roi_params = "{};{};{};{};{};{};{};{};{}".format(ob, roi_origin_position, roi_size, self.view.line_roi.ab_angle,
        #                                                  self.view.line_roi.ac_angle, axis, self.view.line_roi.origin,
        #                                                  self.view.line_roi.ab_vector, self.view.line_roi.ac_vector)
        
        # clipboard_text = "{};{}".format(clipboard_text, roi_params)
        clipboard_text = "{};{}".format(clipboard_text, ob)

        return point, clipboard_text
    
    def scale_point_to_CCF(self, point):
        """
        Returns a tuple (x, y, z) scaled from Item coordinates to CCF coordinates
        
        Point is a tuple with values x, y, z (ordered) 
        """
        vxsize = self.atlas._info[-1]['vxsize'] * 1e6
        p_to_ccf = ((self.view.atlas.shape[1] - point[0]) * vxsize,
                    (self.view.atlas.shape[2] - point[1]) * vxsize,
                    (self.view.atlas.shape[0] - point[2]) * vxsize)
        return p_to_ccf
    
    def scale_vector_to_PIR(self, vector):
        """
        Returns a list representing a vector. The new vector is scaled to CCF coordinate size. Also orients the vector to PIR orientaion.
        
        Vector must me specified as a list
        """
        p_to_ccf = []
        for p in vector:
            p_to_ccf.append(-(p * self.atlas._info[-1]['vxsize'] * 1e6))  # Need to use negative since using PIR orientation
        return p_to_ccf

    def ccf_point_to_view(self, pos):
        """
        This function translates a ccf's position to the view's coordinates.
                
        The pos is a tuple with values x, y, z
        """
        vxsize = self.atlas._info[-1]['vxsize'] * 1e6
        return ((self.view.atlas.shape[1] - (pos[0] / vxsize)) * self.view.scale[0],
                (self.view.atlas.shape[2] - (pos[1] / vxsize)) * self.view.scale[1],
                (self.view.atlas.shape[0] - (pos[2] / vxsize)) * self.view.scale[1])

    def vector_to_view(self, vector):
        """
        Scales vector to view coordinate size. vector is a tuple with x, y, z (in that order)   
        """
        vxsize = self.atlas._info[-1]['vxsize'] * 1e6
        new_point = ((vector[0] / vxsize) * self.view.scale[0],
                     (vector[1] / vxsize) * self.view.scale[1],
                     (vector[2] / vxsize) * self.view.scale[1])  
        return new_point
      
    # These are here to test. Add to coord_arg to test
    # to_pos = self.st_to_tuple(coord_args[5])
    # to_size = self.st_to_tuple(coord_args[6])
    # to_ab_angle = float(coord_args[7])
    # to_ac_angle = float(coord_args[8])
    # orientation = coord_args[9]    
    def coordinateSubmitted(self):
        if self.displayCtrl.params['Orientation'] != "right":
            displayError('Set Coordinate function is only supported with Right orientation')
            return
        
        coord_args = str(self.coordinateCtrl.line.text()).split(';')
        
        vxsize = self.atlas._info[-1]['vxsize'] * 1e6
        x = float(coord_args[0])
        y = float(coord_args[1])
        z = float(coord_args[2])
        
        if len(coord_args) < 3:
            return
        
        if len(coord_args) <= 4:
            # When only 4 points are given, assume point needs to be set using orientation == 'right'
            translated_x = (self.view.atlas.shape[1] - (float(coord_args[0])/vxsize)) * self.view.scale[0] 
            translated_y = (self.view.atlas.shape[2] - (float(coord_args[1])/vxsize)) * self.view.scale[0] 
            translated_z = (self.view.atlas.shape[0] - (float(coord_args[2])/vxsize)) * self.view.scale[0] 
            roi_origin = (translated_x, 0.0)
            to_size = (self.view.atlas.shape[2] * self.view.scale[1], 0.0) 
            to_ab_angle = 90
            to_ac_angle = 0
            target_p1 = translated_z 
            target_p2 = translated_y
        else:
            transform = literal_eval(coord_args[4])

            # Use LIMS matrices to get the origin and vectors of the plane
            M1, M1i = points_to_aff.lims_obj_to_aff(transform)
            origin, ab_vector, ac_vector = points_to_aff.aff_to_origin_and_vectors(M1i)
            
            target_p1, target_p2 = self.get_target_position([x, y, z, 1], M1, ab_vector, ac_vector, vxsize)
            
            # Put the origin and vectors back to view coordinates
            roi_origin = np.array(self.ccf_point_to_view(origin))
            ab_vector = -np.array(self.vector_to_view(ab_vector))
            ac_vector = -np.array(self.vector_to_view(ac_vector))
                
            to_ac_angle = self.view.line_roi.get_ac_angle(ac_vector)
            
            # Where the origin of the ROI should be
            if to_ac_angle > 0:
                roi_origin = ac_vector + roi_origin  
                
            to_size = self.view.line_roi.get_roi_size(ab_vector, ac_vector)
            to_ab_angle = self.view.line_roi.get_ab_angle(ab_vector)
        
        self.view.target.setPos(target_p1, target_p2)
        self.view.line_roi.setPos(pg.Point(roi_origin[0], roi_origin[1]))
        self.view.line_roi.setSize(pg.Point(to_size))
        self.view.line_roi.setAngle(to_ab_angle) 
        self.view.slider.setValue(int(to_ac_angle))
        self.view.target.setVisible(True)  # TODO: keep target visible when coming back to the same slice... how?
       
    def get_target_position(self, ccf_location, M, ab_vector, ac_vector, vxsize):
        """
        Use affine transform matrix M to map ccf coordinate back to original coordinates  
        """
        img_location = np.dot(M, ccf_location)
        
        p1 = (np.linalg.norm(ac_vector) / vxsize * img_location[1]) * self.view.scale[0]
        p2 = (np.linalg.norm(ab_vector) / vxsize * img_location[0]) * self.view.scale[0]
        
        return p1, p2
    

class CoordinatesCtrl(QtGui.QWidget):
    coordinateSubmitted = QtCore.Signal()
    
    def __init__(self, parent=None):
        QtGui.QWidget.__init__(self, parent)
        self.layout = QtGui.QGridLayout()
        self.setLayout(self.layout)
        self.layout.setSpacing(0)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        self.line = QtGui.QLineEdit(self)
        self.line.returnPressed.connect(self.set_coordinate)
        self.layout.addWidget(self.line, 0, 0)

        self.btn = QtGui.QPushButton('Set Coordinate', self)
        self.layout.addWidget(self.btn, 1, 0)
        self.btn.clicked.connect(self.set_coordinate)
    
    def set_coordinate(self):
        errors = self.validate_location()
        if not errors:
            self.coordinateSubmitted.emit()
        else:
            displayError(errors)
            
    def validate_location(self):
        location = self.line.text()
        if location:
            tokens = str(self.line.text()).split(';')
            if len(tokens) < 3:
                return "Coordinate is malformed"
            elif len(tokens) == 3 or len(tokens) == 4:
                errors = self.target_within_range(float(tokens[0]), float(tokens[1]), float(tokens[2])) 
            else:
                errors = self.target_within_range(float(tokens[0]), float(tokens[1]), float(tokens[2]))
                
            return errors
        else:
            return "No coordinate provided"
    
    def target_within_range(self, x, y, z):

        vxsize = atlas._info[-1]['vxsize'] * 1e6
        error = ""
        if z > (self.atlas_shape[2] * vxsize) or z < 0:
            error += "z coordinate {} is not within CCF range".format(z)
        if x > self.atlas_shape[0] * vxsize or x < 0:
            error += " x coordinate {} is not within CCF range".format(x)
        if y > self.atlas_shape[1] * vxsize or y < 0:
            error += " y coordinate {} is not within CCF range".format(y)
        
        return error
    
        
class VolumeSliceView(QtGui.QWidget):
    mouseHovered = QtCore.Signal(object)
    mouseClicked = QtCore.Signal(object)

    def __init__(self, parent=None):
        QtGui.QWidget.__init__(self, parent)
        self.resize(800, 800)
        self.layout = QtGui.QGridLayout()
        self.setLayout(self.layout)
        self.layout.setSpacing(0)
        self.layout.setContentsMargins(0,0,0,0)

        self.w1 = pg.GraphicsLayoutWidget()
        self.w2 = pg.GraphicsLayoutWidget()
        self.view1 = self.w1.addViewBox()
        self.view2 = self.w2.addViewBox()
        self.view1.setAspectLocked()
        self.view2.setAspectLocked()
        self.view1.invertY(False)
        self.view2.invertY(False)
        self.layout.addWidget(self.w1, 0, 0)
        self.layout.addWidget(self.w2, 1, 0)

        self.atlas_view = AtlasSliceView()
        self.atlas_view.sig_slice_changed.connect(self.sliceChanged)
        self.img1 = self.atlas_view.img1
        self.img2 = self.atlas_view.img2
        self.img2.mouseClicked.connect(self.mouseClicked)
        self.view1.addItem(self.img1)
        self.view2.addItem(self.img2)

        self.target = Target()
        self.target.setZValue(5000)
        self.view2.addItem(self.target)
        self.target.setVisible(False)

        self.view1.addItem(self.atlas_view.line_roi, ignoreBounds=True)
        self.layout.addWidget(self.atlas_view.zslider, 2, 0)
        self.layout.addWidget(self.atlas_view.slider, 3, 0)
        self.layout.addWidget(self.atlas_view.lut, 0, 1, 3, 1)

        self.clipboard = QtGui.QApplication.clipboard()
        
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Up"), self, self.slider_up)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Down"), self, self.slider_down)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Left"), self, self.tilt_left)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Right"), self, self.tilt_right)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+1"), self, self.move_left)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+2"), self, self.move_right)

    def slider_up(self):
        self.atlas_view.slider.triggerAction(QtGui.QAbstractSlider.SliderSingleStepAdd)
        
    def slider_down(self):
        self.atlas_view.slider.triggerAction(QtGui.QAbstractSlider.SliderSingleStepSub)
        
    def tilt_left(self):
        self.atlas_view.line_roi.rotate(1)
        
    def tilt_right(self):
        self.atlas_view.line_roi.rotate(-1)
        
    def move_right(self):
        # print '-- Pos'
        # print self.line_roi.pos()
        self.atlas_view.line_roi.setPos((self.atlas_view.line_roi.pos().x() + .0001, self.atlas_view.line_roi.pos().y()))
        
    def move_left(self):
        # print '-- Pos'
        # print self.line_roi.pos()
        self.atlas_view.line_roi.setPos((self.atlas_view.line_roi.pos().x() - .0001, self.atlas_view.line_roi.pos().y()))

    def setData(self, image, label, scale=None):
        self.atlas_view.set_data(image, label, scale)
        self.view1.autoRange(items=[self.img1.atlasImg])

    def sliceChanged(self):
        self.view2.autoRange(items=[self.img2.atlasImg])
        self.target.setVisible(False)
        self.w1.viewport().repaint()  # repaint immediately to avoid processing more mouse events before next repaint
        self.w2.viewport().repaint()

    def closeEvent(self, ev):
        self.imv1.close()
        self.imv2.close()
        self.atlas_view.close()



class Target(pg.GraphicsObject):
    def __init__(self, movable=True):
        pg.GraphicsObject.__init__(self)
        self._bounds = None
        self.color = (255, 255, 0)

    def boundingRect(self):
        if self._bounds is None:
            # too slow!
            w = self.pixelLength(pg.Point(1, 0))
            if w is None:
                return QtCore.QRectF()
            h = self.pixelLength(pg.Point(0, 1))
            # o = self.mapToScene(QtCore.QPointF(0, 0))
            # w = abs(1.0 / (self.mapToScene(QtCore.QPointF(1, 0)) - o).x())
            # h = abs(1.0 / (self.mapToScene(QtCore.QPointF(0, 1)) - o).y())
            self._px = (w, h)
            w *= 21
            h *= 21
            self._bounds = QtCore.QRectF(-w, -h, w*2, h*2)
        return self._bounds

    def viewTransformChanged(self):
        self._bounds = None
        self.prepareGeometryChange()

    def paint(self, p, *args):
        p.setRenderHint(p.Antialiasing)
        px, py = self._px
        w = 4 * px
        h = 4 * py
        r = QtCore.QRectF(-w, -h, w*2, h*2)
        p.setPen(pg.mkPen(self.color))
        p.setBrush(pg.mkBrush(0, 0, 255, 100))
        p.drawEllipse(r)
        p.drawLine(pg.Point(-w*2, 0), pg.Point(w*2, 0))
        p.drawLine(pg.Point(0, -h*2), pg.Point(0, h*2))


def displayError(error):
    print error
    err = QtGui.QErrorMessage()
    err.showMessage(error)
    err.exec_()


def displayMessage(message):
    box = QtGui.QMessageBox()
    box.setIcon(QtGui.QMessageBox.Information)
    box.setText(message)
    box.setStandardButtons(QtGui.QMessageBox.Ok)
    box.exec_()


if __name__ == '__main__':

    app = pg.mkQApp()

    v = AtlasViewer()
    v.setWindowTitle('CCF Viewer')
    v.show()

    atlas_data = CCFAtlasData(image_cache_file='ccf.ma', label_cache_file='ccf_label.ma')
    
    if atlas_data.image is None or atlas_data.label is None:
        # nothing loaded from cache
        displayMessage('Please Select NRRD Atlas File')
        nrrd_file = QtGui.QFileDialog.getOpenFileName(None, "Select NRRD atlas file")
        with pg.BusyCursor():
            atlas_data.load_image_data(nrrd_file)
        
        displayMessage('Select NRRD annotation file')
        nrrd_file = QtGui.QFileDialog.getOpenFileName(None, "Select NRRD annotation file")

        displayMessage('Select ontology file (json)')
        onto_file = QtGui.QFileDialog.getOpenFileName(None, "Select ontology file (json)")
        
        atlas_data.load_label_data(nrrd_file, onto_file)
    
    v.set_data(atlas_data)

    if sys.flags.interactive == 0:
        app.exec_()
