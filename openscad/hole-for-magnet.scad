$fn = 64; // Resolution of the circle

difference() {
    // Load original base STL file
    // Note: this assumes that in the original STL the base is centered at X=0, Y=0 with the bottom at Z=0
    import("basetta_centrata.stl");
    
    // Subtract cylinder from the center of the base (5.2mm diameter, 2.2mm height)
    // Z starts at -0.1 to ensure the bottom face is open
    translate([0, 0, -0.1])
        cylinder(r = 2.6, h = 2.2 + 0.1);
}
