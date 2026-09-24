// rustfmt-blank_lines_before_control_flow_statements: 2
// rustfmt-blank_lines_upper_bound: 1
fn sample(n: u32) {
    let m = n;


    if m > 0 {
        println!("a");
    }
    let k = m + 1;


    for _ in 0..k {
        println!("b");
    }


    match k {
        _ => {}
    }
    let q = k + 1;


    while q > 0 {
        q -= 1;
    }
}
