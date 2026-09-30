// rustfmt-blank_lines_after_control_flow_statements: 1
// rustfmt-blank_lines_between_items: 2
fn outer(n: u32) {
    if n > 0 {
        println!("a");
    }
    #[allow(dead_code)]
    fn inner() {}
    let m = n;
}
