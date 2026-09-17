import Foundation
import SceneKit
import ModelIO
import SceneKit.ModelIO
import AppKit
import Metal

let base = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("outputs/tile11-sample")
let scene = SCNScene(mdlAsset: MDLAsset(url: base.appendingPathComponent("source/11.obj")))
let pivot = SCNNode()
for n in scene.rootNode.childNodes { n.removeFromParentNode(); pivot.addChildNode(n) }
scene.rootNode.addChildNode(pivot)
let (lo, hi) = pivot.boundingBox
print("BOUNDS",lo,hi)
let center = SCNVector3((lo.x+hi.x)/2,(lo.y+hi.y)/2,(lo.z+hi.z)/2)
let size = max(hi.x-lo.x,max(hi.y-lo.y,hi.z-lo.z))
for n in pivot.childNodes {
 n.position = SCNVector3(n.position.x-center.x,n.position.y-center.y,n.position.z-center.z)
 n.enumerateChildNodes { node,_ in
  for mat in node.geometry?.materials ?? [] { mat.lightingModel = .constant; mat.isDoubleSided = true }
 }
 for mat in n.geometry?.materials ?? [] { mat.lightingModel = .constant; mat.isDoubleSided = true }
}
pivot.scale = SCNVector3(2/size,2/size,2/size)
scene.background.contents = NSColor(calibratedWhite:0.88,alpha:1)
let camera = SCNNode();camera.camera=SCNCamera();camera.camera!.usesOrthographicProjection=true;camera.camera!.orthographicScale=1.5
camera.position=SCNVector3(0,0.3,5);camera.look(at:SCNVector3(0,0,0));scene.rootNode.addChildNode(camera)
let renderer = SCNRenderer(device: MTLCreateSystemDefaultDevice(),options:nil);renderer.scene=scene;renderer.pointOfView=camera
let frames=base.appendingPathComponent("frames");try FileManager.default.createDirectory(at:frames,withIntermediateDirectories:true)
let count=CommandLine.arguments.contains("--full") ? 450 : 1
for i in 0..<count {
 autoreleasepool {
  pivot.eulerAngles.y=CGFloat(i)*2*CGFloat.pi/450
  let image=renderer.snapshot(atTime:Double(i)/30,with:CGSize(width:1920,height:1080),antialiasingMode:.multisampling4X)
  let bitmap=NSBitmapImageRep(data:image.tiffRepresentation!)!
  try! bitmap.representation(using:.png,properties:[:])!.write(to:frames.appendingPathComponent(String(format:"%04d.png",i)))
 }
 if i%30==0 { print("Frame",i);fflush(stdout) }
}
