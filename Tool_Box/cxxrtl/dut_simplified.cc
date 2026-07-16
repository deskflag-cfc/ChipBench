// Hand-written "simplified" CXXRTL model of the LFSR in Tool_Box/verilog/ref.sv,
// following the contract in "Ref Model Gen/gen_cxxrtl_prompt.txt":
// a standalone struct (no cxxrtl::module inheritance), void eval() / bool commit(),
// wire<1> p_clk driven by the harness on both curr and next, eval() called
// exactly once per clock level.
#include <cxxrtl/cxxrtl.h>

namespace cxxrtl_design {

struct p_TopModule {
    cxxrtl::wire<1> p_clk;
    cxxrtl::value<1> p_rst__n;
    cxxrtl::value<4> p_Q;

    cxxrtl::value<4> state_q;

    void eval() {
        bool posedge_clk = p_clk.curr.data[0] & 1;
        if (posedge_clk) {
            if (!(p_rst__n.data[0] & 1)) {
                state_q.data[0] = 0;
            } else {
                uint32_t q = state_q.data[0] & 0xF;
                state_q.data[0] = (((~q & 1) << 3) | (q >> 1)) & 0xF;
            }
        }
        p_Q.data[0] = state_q.data[0] & 0xF;
    }

    bool commit() {
        return false;
    }
};

} // namespace cxxrtl_design
