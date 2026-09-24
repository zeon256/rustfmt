// rustfmt-blank_lines_before_control_flow_statements: 1
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
}

fn first_in_block() {
    for _ in 0..1 {
        println!("first");
    }

    if true {
        println!("not first");
    }
}

fn nested_first_if(n: u32) {
    if n > 0 {
        if n > 1 {
            println!("a");
        }

        if n > 2 {
            println!("b");
        }
    }
}

fn later_if_in_outer_body(n: u32) {
    for _ in 0..2 {
        println!("a");

        if n > 0 {
            println!("c");
        }

        while n > 0 {
            n -= 1;
        }
    }
}

fn if_else(n: u32) {
    if n > 0 {
        println!("a");
    } else {
        println!("b");
    }
}

fn embedded_expressions(n: u32) {
    let x = if n > 0 { 1 } else { 2 };
    let y = match n {
        _ => 3,
    };
    let z = x + y;
    println!("{z}");
}

fn labeled_loop(n: u32) {
    'outer: loop {
        for _ in 0..n {
            break 'outer;
        }
    }
}

fn semi_kind(n: u32) {
    match n {
        _ => {}
    };

    if n > 0 {
        println!("s");
    }
}

fn comment_and_attribute(n: u32) {
    let m = n;

    // Keep this comment attached to the match.
    match m {
        _ => {}
    }

    #[allow(unused_variables)]
    while false {
        println!("x");
    }
}

fn skipped_statement(n: u32) {
    let m = n;

    #[rustfmt::skip]
    match m {
        _ => {}
    }
}
