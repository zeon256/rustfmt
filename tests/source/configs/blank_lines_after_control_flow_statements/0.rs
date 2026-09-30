// rustfmt-blank_lines_after_control_flow_statements: 0
// rustfmt-blank_lines_lower_bound: 1
fn sample(n: u32) {
    let m = n;
    if m > 0 {
        println!("a");



        println!("b");
    }


    for _ in 0..m {
        println!("c");



        println!("d");
    }
    let q = m + 1;
    while q > 0 {
        q -= 1;
    }


    match q {
        _ => {}
    }
    let r = q + 1;
}
