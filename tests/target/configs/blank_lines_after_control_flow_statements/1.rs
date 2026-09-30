// rustfmt-blank_lines_after_control_flow_statements: 1
fn five_kinds(n: u32) {
    let m = n + 1;
    if m > 0 {
        println!("if");
    }

    match m {
        _ => println!("match"),
    }

    for i in 0..m {
        println!("{i}");
    }

    while m > 0 {
        println!("while");
    }

    loop {
        break;
    }

    let q = m;
}

fn first_in_block() {
    for _ in 0..1 {
        println!("first");
    }
}

fn if_else(n: u32) {
    if n > 0 {
        println!("a");
    } else {
        println!("b");
    }

    let k = n;
}

fn labeled_and_semi(n: u32) {
    'outer: loop {
        for _ in 0..n {
            break 'outer;
        }
    }

    match n {
        _ => {}
    };

    let s = n;
}

fn nested_blocks(n: u32) {
    if n > 0 {
        {
            y();
        }
        let z = n;
    }

    let k = n;
}

fn embedded_expressions(n: u32) {
    let x = if n > 0 { 1 } else { 2 };
    let y = match n {
        _ => 3,
    };
    let z = x + y;
    println!("{z}");
}

fn macros(n: u32) {
    if n > 0 {
        println!("m");
    }

    println!("{}", n);
    vec![n];
    let t = n;
}

fn local_item(n: u32) {
    if n > 0 {
        println!("li");
    }

    #[allow(dead_code)]
    fn inner() {}
    let m = n;
}

fn comment_and_attribute(n: u32) {
    let m = n;
    while false {
        println!("w");
    }

    // Keep this comment attached to the let.
    #[allow(unused_variables)]
    let r = m;
}

fn skipped_predecessor(n: u32) {
    let m = n;

    #[rustfmt::skip]
    match m {
        _ => {}
    }
    let k = n;
}

fn skipped_successor(n: u32) {
    let m = n;
    if m > 0 {
        println!("s");
    }

    #[rustfmt::skip]
    let k = m;
    if m > 1 {
        println!("t");
    }

    let f = m;
}

fn redundant_item_semi(n: u32) {
    if n > 0 {
        println!("i");
    }

    fn inner() {};
    let m = n;
}

fn reordered_uses(n: u32) {
    let m = n;
    while false {
        println!("u");
    }

    use a;
    use b;
    let c = m;
}
