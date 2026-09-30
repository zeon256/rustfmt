// rustfmt-blank_lines_after_control_flow_statements: 0
// rustfmt-blank_lines_before_control_flow_statements: 1
fn sample(n: u32) {
    let m = n;
    if m > 0 {
        println!("a");
    }
    match m {
        _ => {}
    }
    let k = m;
}
